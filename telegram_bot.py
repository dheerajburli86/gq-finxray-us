import os
import logging
import asyncio
from supabase import create_client
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── User registration ─────────────────────────────────────────────────────────
def get_or_create_user(chat_id: str):
    try:
        result = supabase.table("users") \
            .select("*") \
            .eq("telegram_chat_id", str(chat_id)) \
            .execute()
        if result.data:
            return result.data[0]
        new_user = supabase.table("users").insert({
            "telegram_chat_id": str(chat_id)
        }).execute()
        supabase.table("user_preferences").insert({
            "user_id": new_user.data[0]["id"],
            "min_impact": "LOW",
            "max_alerts_per_day": 20
        }).execute()
        return new_user.data[0]
    except Exception as e:
        logger.error(f"Error getting/creating user: {e}")
        return None

def get_user_watchlist(user_id: str):
    try:
        result = supabase.table("watchlists") \
            .select("ticker, company_name") \
            .eq("user_id", user_id) \
            .execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting watchlist: {e}")
        return []

def get_user_preferences(user_id: str):
    try:
        result = supabase.table("user_preferences") \
            .select("*") \
            .eq("user_id", user_id) \
            .execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting preferences: {e}")
        return None

# ── Commands ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_or_create_user(chat_id)
    if not user:
        await update.message.reply_text("Something went wrong. Please try again.")
        return
    welcome = (
        "👋 Welcome to *GQ FinXray US*\n\n"
        "Your personal AI-powered US stock market alert system.\n\n"
        "I monitor SEC filings, insider trades, and breaking news in real time "
        "and send you instant alerts for stocks you care about.\n\n"
        "*Getting started:*\n"
        "➕ /add AAPL — subscribe to a stock\n"
        "➖ /remove AAPL — unsubscribe\n"
        "📋 /list — view your watchlist\n"
        "⚙️ /setimpact — set minimum alert level\n"
        "❓ /help — show all commands\n\n"
        "_Start by adding your first stock ticker._"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")

async def add_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_or_create_user(chat_id)
    if not user:
        await update.message.reply_text("Something went wrong. Please try again.")
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide a ticker symbol.\nExample: /add AAPL"
        )
        return

    ticker = context.args[0].upper().strip()

    # Validate ticker — letters only, max 5 chars
    if not ticker.isalpha() or len(ticker) > 5:
        await update.message.reply_text(
            f"❌ '{ticker}' doesn't look like a valid ticker.\n"
            "Use the stock symbol like AAPL, TSLA, MSFT."
        )
        return

    try:
        # Check if already subscribed
        existing = supabase.table("watchlists") \
            .select("id") \
            .eq("user_id", user["id"]) \
            .eq("ticker", ticker) \
            .execute()
        if existing.data:
            await update.message.reply_text(
                f"✅ You're already subscribed to *{ticker}*.",
                parse_mode="Markdown"
            )
            return

        # Add to watchlist
        supabase.table("watchlists").insert({
            "user_id": user["id"],
            "ticker": ticker
        }).execute()

        await update.message.reply_text(
            f"✅ Added *{ticker}* to your watchlist.\n"
            f"You'll receive alerts whenever something important happens.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error adding ticker: {e}")
        await update.message.reply_text("Something went wrong. Please try again.")

async def remove_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_or_create_user(chat_id)
    if not user:
        await update.message.reply_text("Something went wrong. Please try again.")
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide a ticker symbol.\nExample: /remove AAPL"
        )
        return

    ticker = context.args[0].upper().strip()

    try:
        result = supabase.table("watchlists") \
            .select("id") \
            .eq("user_id", user["id"]) \
            .eq("ticker", ticker) \
            .execute()

        if not result.data:
            await update.message.reply_text(
                f"❌ You're not subscribed to *{ticker}*.",
                parse_mode="Markdown"
            )
            return

        supabase.table("watchlists") \
            .delete() \
            .eq("user_id", user["id"]) \
            .eq("ticker", ticker) \
            .execute()

        await update.message.reply_text(
            f"✅ Removed *{ticker}* from your watchlist.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error removing ticker: {e}")
        await update.message.reply_text("Something went wrong. Please try again.")

async def list_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_or_create_user(chat_id)
    if not user:
        await update.message.reply_text("Something went wrong. Please try again.")
        return

    watchlist = get_user_watchlist(user["id"])
    prefs = get_user_preferences(user["id"])

    if not watchlist:
        await update.message.reply_text(
            "📋 Your watchlist is empty.\n"
            "Add stocks with /add AAPL"
        )
        return

    tickers = [w["ticker"] for w in watchlist]
    min_impact = prefs["min_impact"] if prefs else "LOW"

    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
    emoji = impact_emoji.get(min_impact, "🟢")

    msg = f"📋 *Your Watchlist* ({len(tickers)} stocks)\n\n"
    for ticker in tickers:
        msg += f"• {ticker}\n"
    msg += f"\n⚙️ Alert level: {emoji} {min_impact} and above\n"
    msg += "_Use /setimpact to change alert level_"

    await update.message.reply_text(msg, parse_mode="Markdown")

async def set_impact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_or_create_user(chat_id)
    if not user:
        await update.message.reply_text("Something went wrong. Please try again.")
        return

    keyboard = [
        [
            InlineKeyboardButton("🔴 HIGH only", callback_data="impact_HIGH"),
        ],
        [
            InlineKeyboardButton("🟡 MEDIUM and above", callback_data="impact_MEDIUM"),
        ],
        [
            InlineKeyboardButton("🟢 ALL alerts", callback_data="impact_LOW"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⚙️ *Set minimum alert level*\n\n"
        "🔴 *HIGH only* — CEO changes, acquisitions, bankruptcy, earnings surprises\n"
        "🟡 *MEDIUM and above* — Above + board meetings, analyst days, product launches\n"
        "🟢 *ALL alerts* — Everything including routine filings\n\n"
        "What level do you want?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def impact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    user = get_or_create_user(chat_id)
    if not user:
        await query.edit_message_text("Something went wrong. Please try again.")
        return

    impact = query.data.replace("impact_", "")
    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
    impact_label = {"HIGH": "HIGH only", "MEDIUM": "MEDIUM and above", "LOW": "ALL alerts"}

    try:
        supabase.table("user_preferences") \
            .update({"min_impact": impact}) \
            .eq("user_id", user["id"]) \
            .execute()

        await query.edit_message_text(
            f"✅ Alert level set to {impact_emoji[impact]} *{impact_label[impact]}*\n\n"
            f"You'll only receive alerts at this level or higher.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error setting impact: {e}")
        await query.edit_message_text("Something went wrong. Please try again.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "❓ *GQ FinXray US — Commands*\n\n"
        "➕ /add TICKER — Subscribe to a stock\n"
        "    Example: /add AAPL\n\n"
        "➖ /remove TICKER — Unsubscribe from a stock\n"
        "    Example: /remove TSLA\n\n"
        "📋 /list — View your watchlist and settings\n\n"
        "⚙️ /setimpact — Set minimum alert level\n"
        "    HIGH = most important events only\n"
        "    MEDIUM = important + upcoming events\n"
        "    LOW = everything\n\n"
        "❓ /help — Show this message\n\n"
        "*Alert types you'll receive:*\n"
        "🔴 HIGH — CEO changes, acquisitions, earnings surprises\n"
        "🟡 MEDIUM — Board meetings, analyst days, product launches\n"
        "🟢 LOW — Routine filings and compliance notices\n\n"
        "*Sources monitored:*\n"
        "📋 SEC EDGAR — 8-K filings, Form 4 insider trades, S-1 IPOs\n"
        "📰 CNBC, Bloomberg, MarketWatch — Breaking news"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ── Alert delivery function (called by pipeline) ──────────────────────────────
async def send_alert(bot, chat_id: str, alert: dict):
    impact = alert.get("impact", "LOW")
    ticker = alert.get("ticker", "UNKNOWN")
    summary = alert.get("summary", "")
    source = alert.get("source", "SEC_EDGAR")
    filing_type = alert.get("filing_type", "")

    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
    source_label = {
        "SEC_EDGAR": "SEC EDGAR",
        "CNBC": "CNBC",
        "BLOOMBERG": "Bloomberg",
        "MARKETWATCH": "MarketWatch",
        "YAHOO_FINANCE": "Yahoo Finance"
    }

    emoji = impact_emoji.get(impact, "🟢")
    source_name = source_label.get(source, source)

    msg = (
        f"{emoji} *{impact} IMPACT — ${ticker}*\n\n"
        f"{summary}\n\n"
        f"📋 {source_name}"
    )

    if filing_type:
        msg += f" · {filing_type}"

    from datetime import datetime
    msg += f" · {datetime.now().strftime('%I:%M %p IST')}"

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="Markdown"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send alert to {chat_id}: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_ticker))
    app.add_handler(CommandHandler("remove", remove_ticker))
    app.add_handler(CommandHandler("list", list_watchlist))
    app.add_handler(CommandHandler("setimpact", set_impact))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(impact_callback, pattern="^impact_"))

    print("[TELEGRAM BOT] Starting GQ FinXray US bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()