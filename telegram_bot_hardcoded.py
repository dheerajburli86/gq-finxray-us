#!/usr/bin/env python3
"""
telegram_bot.py
GQ FinXray US — Hardcoded user dj_12312
"""

import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode
from supabase import create_client

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Hardcoded authorized user
AUTHORIZED_USERNAME = "dj_12312"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ── Helper Functions ──────────────────────────────────────────────────────────
def get_watchlist(chat_id: int):
    """Get watchlist for this chat (stored in memory for now)."""
    if not hasattr(get_watchlist, 'data'):
        get_watchlist.data = {}
    return get_watchlist.data.get(chat_id, [])


def add_stock(chat_id: int, ticker: str):
    """Add stock to watchlist."""
    if not hasattr(get_watchlist, 'data'):
        get_watchlist.data = {}
    if chat_id not in get_watchlist.data:
        get_watchlist.data[chat_id] = []

    ticker = ticker.upper()
    if ticker not in get_watchlist.data[chat_id]:
        get_watchlist.data[chat_id].append(ticker)
        return True
    return None  # Already exists


def remove_stock(chat_id: int, ticker: str):
    """Remove stock from watchlist."""
    if not hasattr(get_watchlist, 'data'):
        get_watchlist.data = {}
    if chat_id not in get_watchlist.data:
        return False

    ticker = ticker.upper()
    if ticker in get_watchlist.data[chat_id]:
        get_watchlist.data[chat_id].remove(ticker)
        return True
    return False


def stock_exists(ticker: str):
    """Check if stock exists in stocks table."""
    try:
        result = supabase.table("stocks").select("ticker").eq("ticker", ticker.upper()).limit(1).execute()
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"[DB] Failed to check stock {ticker}: {e}")
        return False


# ── Telegram Command Handlers ──────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — authenticate user."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    await update.message.reply_text(
        f"👋 Welcome to *GQ FinXray US*, {user.first_name}!\n\n"
        f"You're authenticated as *{AUTHORIZED_USERNAME}*\n\n"
        f"Now add your desired stocks:\n\n"
        f"/add STOCK — Add stock to your watchlist\n"
        f"/remove STOCK — Remove stock\n"
        f"/list — See your stocks\n"
        f"/help — Show all commands",
        parse_mode=ParseMode.MARKDOWN
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add STOCK — add stock to watchlist."""
    chat_id = update.effective_chat.id

    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "Usage: /add STOCK\n_Example: /add AAPL_",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    ticker = context.args[0].upper()

    # Check if stock exists
    if not stock_exists(ticker):
        await update.message.reply_text(
            f"❌ Stock *{ticker}* not found.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Add to watchlist
    result = add_stock(chat_id, ticker)
    if result is True:
        await update.message.reply_text(
            f"✅ Added *{ticker}* to your watchlist!",
            parse_mode=ParseMode.MARKDOWN
        )
    elif result is None:
        await update.message.reply_text(
            f"ℹ️ *{ticker}* already in your watchlist.",
            parse_mode=ParseMode.MARKDOWN
        )


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove STOCK — remove stock from watchlist."""
    chat_id = update.effective_chat.id

    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "Usage: /remove STOCK\n_Example: /remove AAPL_",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    ticker = context.args[0].upper()

    result = remove_stock(chat_id, ticker)
    if result:
        await update.message.reply_text(
            f"✅ Removed *{ticker}* from your watchlist.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ *{ticker}* not in your watchlist.",
            parse_mode=ParseMode.MARKDOWN
        )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list — show user's watchlist."""
    chat_id = update.effective_chat.id
    watchlist = get_watchlist(chat_id)

    if not watchlist:
        await update.message.reply_text(
            "📋 Your watchlist is empty.\n\nUse /add STOCK to add stocks.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = [f"📋 *Your Watchlist ({len(watchlist)} stocks):*\n"]
    for ticker in sorted(watchlist):
        lines.append(f"• *{ticker}*")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help — show available commands."""
    help_text = """
🤖 *GQ FinXray US*

/start — Get started
/add STOCK — Add stock to watchlist
/remove STOCK — Remove stock
/list — Show your watchlist
/help — Show this message
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    """Start the Telegram bot."""
    print("""
╔═══════════════════════════════════════════════════════════════╗
║  GQ FinXray US — Watchlist Bot                               ║
║  Authorized User: dj_12312                                   ║
╚═══════════════════════════════════════════════════════════════╝
    """)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("help", help_command))

    print("[BOT] Starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
