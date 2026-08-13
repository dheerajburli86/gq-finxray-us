#!/usr/bin/env python3
"""
telegram_bot.py
GQ FinXray US — watchlist management bot.

Commands:
  /start        — register this chat against your account
  /add TICKER   — add a stock to your watchlist
  /remove TICKER— remove a stock from your watchlist
  /list         — show your watchlist
  /help         — show commands

Run with:  python telegram_bot.py      (1 replica only — see main())
"""

import os
import logging

from dotenv import load_dotenv
from supabase import create_client
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    InlineQueryHandler, MessageHandler, filters,
)

load_dotenv()

# TELEGRAM_TOKEN is what Railway has; TELEGRAM_BOT_TOKEN accepted as an alias so
# the same file runs unchanged in either environment.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Onboarding a user is a Railway variable edit, not a code change + redeploy.
AUTHORIZED_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("AUTHORIZED_USERNAMES", "dj_12312").split(",")
    if u.strip()
}

MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST", "50"))

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit(
        "Missing env vars. Need TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_KEY."
    )

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


# ── users ─────────────────────────────────────────────────────────────────────
def is_authorized(username: str) -> bool:
    return bool(username) and username.lstrip("@").lower() in AUTHORIZED_USERNAMES


def get_user_by_chat_id(chat_id):
    # telegram_chat_id is TEXT in Postgres. Passing an int works by coercion on
    # the way in but not on the way back out of a filter -- always send a string.
    try:
        r = (supabase.table("users").select("*")
             .eq("telegram_chat_id", str(chat_id)).limit(1).execute())
        return r.data[0] if r.data else None
    except Exception as e:
        log.error("[DB] lookup by chat_id %s failed: %s", chat_id, e)
        return None


def get_user_by_username(username):
    if not username:
        return None
    try:
        # ilike, not eq: the unique index is on lower(telegram_username), so a
        # case-sensitive eq can miss a row that the index will still refuse to
        # let us insert alongside.
        r = (supabase.table("users").select("*")
             .ilike("telegram_username", username.lstrip("@")).limit(1).execute())
        return r.data[0] if r.data else None
    except Exception as e:
        log.error("[DB] lookup by username %s failed: %s", username, e)
        return None


def get_or_create_user(chat_id, username=None, display_name=None):
    """Return this chat's users row, adopting or creating as required.

    Three paths, in order. Skipping the middle one is what broke the old bot:
    it looked users up by chat_id ONLY, so a row seeded by hand (username set,
    chat_id still NULL) was invisible to it. It fell through to INSERT, hit the
    unique index on lower(telegram_username), and returned an error -- for ever,
    no matter how many times the user pressed /start.
    """
    chat_id = str(chat_id)
    uname = (username or "").lstrip("@").strip().lower() or None

    # 1. returning user, keyed by chat_id
    row = get_user_by_chat_id(chat_id)
    if row:
        patch = {}
        if uname and (row.get("telegram_username") or "").lower() != uname:
            patch["telegram_username"] = uname
        if display_name and row.get("display_name") != display_name:
            patch["display_name"] = display_name
        if not row.get("is_active"):
            patch["is_active"] = True
        if patch:
            try:
                supabase.table("users").update(patch).eq("id", row["id"]).execute()
                row.update(patch)
            except Exception as e:
                log.warning("[DB] could not refresh %s: %s", row["id"], e)
        return row

    # 2. row seeded by hand / by an earlier account — adopt it, keeping its
    #    uuid so the watchlist hanging off that uuid survives
    if uname:
        row = get_user_by_username(uname)
        if row:
            patch = {"telegram_chat_id": chat_id,
                     "telegram_user_id": chat_id,
                     "is_active": True}
            if display_name:
                patch["display_name"] = display_name
            try:
                supabase.table("users").update(patch).eq("id", row["id"]).execute()
                row.update(patch)
                log.info("[DB] adopted existing row %s for @%s", row["id"], uname)
                return row
            except Exception as e:
                log.error("[DB] adopt failed for @%s: %s", uname, e)
                return None

    # 3. genuinely new user
    payload = {"telegram_chat_id": chat_id,
               "telegram_user_id": chat_id,
               "is_active": True}
    if uname:
        payload["telegram_username"] = uname
    if display_name:
        payload["display_name"] = display_name
    try:
        r = supabase.table("users").insert(payload).execute()
        log.info("[DB] created user for chat %s (@%s)", chat_id, uname)
        return r.data[0] if r.data else None
    except Exception as e:
        # Lost a race with a concurrent /start, or an index we did not expect.
        # Re-read rather than surfacing an error the user cannot act on.
        if "duplicate" not in str(e).lower() and "23505" not in str(e):
            log.error("[DB] insert failed for chat %s: %s", chat_id, e)
            return None
        log.warning("[DB] insert raced (%s) — re-reading", e)
        return get_user_by_chat_id(chat_id) or get_user_by_username(uname)


# ── stocks & watchlists ───────────────────────────────────────────────────────
def get_stock_by_ticker(ticker):
    try:
        r = (supabase.table("stocks").select("*")
             .eq("ticker", ticker.upper()).limit(1).execute())
        return r.data[0] if r.data else None
    except Exception as e:
        log.error("[DB] stock lookup %s failed: %s", ticker, e)
        return None


def search_stocks_by_prefix(prefix, limit=8):
    try:
        r = (supabase.table("stocks").select("ticker, sector, exchange")
             .ilike("ticker", f"{prefix.upper()}%").limit(limit).execute())
        return r.data or []
    except Exception as e:
        log.error("[DB] stock search %s failed: %s", prefix, e)
        return []


def get_user_watchlist(user_id):
    try:
        # ORDER BY created_at. The column on this table is created_at, not
        # added_at -- ordering by a column that does not exist makes PostgREST
        # return an error, which the caller reads as "empty watchlist".
        r = (supabase.table("watchlists").select("ticker")
             .eq("user_id", user_id).order("created_at").execute())
        return [row["ticker"] for row in (r.data or [])]
    except Exception as e:
        log.error("[DB] watchlist fetch for %s failed: %s", user_id, e)
        return []


def add_to_watchlist(user_id, ticker):
    """True = added, None = already there, False = failed, 'FULL' = at cap."""
    ticker = ticker.upper()
    current = get_user_watchlist(user_id)
    if ticker in current:
        return None
    if len(current) >= MAX_WATCHLIST:
        return "FULL"
    try:
        supabase.table("watchlists").insert(
            {"user_id": user_id, "ticker": ticker}
        ).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "23505" in str(e):
            return None
        log.error("[DB] add %s for %s failed: %s", ticker, user_id, e)
        return False


def remove_from_watchlist(user_id, ticker):
    try:
        r = (supabase.table("watchlists").delete()
             .eq("user_id", user_id).eq("ticker", ticker.upper()).execute())
        return bool(r.data)
    except Exception as e:
        log.error("[DB] remove %s for %s failed: %s", ticker, user_id, e)
        return False


# ── handlers ──────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    chat_id = update.effective_chat.id

    # Telegram already tells us the username. Reading it straight off the
    # account removes a whole class of onboarding failure: someone typing a name
    # that does not match their account and landing on a row their watchlist is
    # not attached to.
    username = (tg_user.username or "").lstrip("@")

    if username and not is_authorized(username):
        await update.message.reply_text(
            f"❌ @{username} isn't on the access list.\n"
            "Ask your GQ admin to add you, then send /start again."
        )
        return

    if not username:
        await update.message.reply_text(
            "👋 Welcome to *GQ FinXray US*\n\n"
            "Your Telegram account has no username set, so I can't identify you.\n"
            "Set one in Telegram → Settings → Username, then send /start again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    user = get_or_create_user(chat_id, username=username,
                             display_name=tg_user.full_name)
    if not user:
        await update.message.reply_text(
            "⚠️ Couldn't set up your account. This has been logged — "
            "tell your admin if it keeps happening."
        )
        return

    context.user_data["user_id"] = user["id"]
    n = len(get_user_watchlist(user["id"]))
    await update.message.reply_text(
        f"✅ You're registered as *@{username}*\n\n"
        f"Watchlist: *{n}* stock{'' if n == 1 else 's'}\n\n"
        "Alerts for these tickers will arrive here automatically.\n"
        "Use /help to see what else I can do.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(update.effective_chat.id)
    if not user or not user.get("is_active"):
        await update.message.reply_text("❌ Not registered. Send /start first.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/add AAPL`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    ticker = context.args[0].upper()
    stock = get_stock_by_ticker(ticker)
    if not stock:
        await update.message.reply_text(
            f"❌ *{ticker}* isn't a ticker I cover. Check the spelling.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    result = add_to_watchlist(user["id"], ticker)
    if result is True:
        await update.message.reply_text(
            f"✅ Added *{ticker}* — {stock.get('sector') or 'Unknown sector'}\n\n"
            "You'll get alerts for it from now on.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif result is None:
        await update.message.reply_text(f"ℹ️ *{ticker}* is already on your list.",
                                        parse_mode=ParseMode.MARKDOWN)
    elif result == "FULL":
        await update.message.reply_text(
            f"❌ Watchlist is full ({MAX_WATCHLIST}). Remove one first."
        )
    else:
        await update.message.reply_text(f"❌ Couldn't add *{ticker}*. Try again.",
                                        parse_mode=ParseMode.MARKDOWN)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(update.effective_chat.id)
    if not user or not user.get("is_active"):
        await update.message.reply_text("❌ Not registered. Send /start first.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/remove AAPL`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    ticker = context.args[0].upper()
    if remove_from_watchlist(user["id"], ticker):
        await update.message.reply_text(f"✅ Removed *{ticker}*.",
                                        parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ *{ticker}* isn't on your list.",
                                        parse_mode=ParseMode.MARKDOWN)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(update.effective_chat.id)
    if not user or not user.get("is_active"):
        await update.message.reply_text("❌ Not registered. Send /start first.")
        return

    watchlist = get_user_watchlist(user["id"])
    if not watchlist:
        await update.message.reply_text(
            "📋 Your watchlist is empty.\n\nAdd one with `/add AAPL`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"📋 *Your watchlist — {len(watchlist)} stock"
             f"{'' if len(watchlist) == 1 else 's'}*\n"]
    for ticker in watchlist:
        stock = get_stock_by_ticker(ticker)
        if stock:
            lines.append(f"• *{ticker}* ({stock.get('exchange') or '?'}) — "
                         f"{stock.get('sector') or 'Unknown'}")
        else:
            lines.append(f"• *{ticker}*")
    lines.append("\n_Remove one with /remove TICKER_")
    await update.message.reply_text("\n".join(lines),
                                    parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *GQ FinXray US*\n\n"
        "*Commands*\n"
        "/start — register this chat\n"
        "/add TICKER — add a stock (e.g. `/add AAPL`)\n"
        "/remove TICKER — remove a stock\n"
        "/list — show your watchlist\n"
        "/help — this message\n\n"
        "*How it works*\n"
        "Alerts are filtered to your watchlist, and only MEDIUM and HIGH "
        f"impact items reach you. Up to {MAX_WATCHLIST} tickers.\n\n"
        "_Tickers are case-insensitive — aapl works as well as AAPL._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bare ticker as a shortcut for /add, otherwise point at /help."""
    text = (update.message.text or "").strip().upper()
    if text.isalpha() and 1 <= len(text) <= 5 and get_stock_by_ticker(text):
        context.args = [text]
        await add_command(update, context)
        return
    await update.message.reply_text("Use /help to see what I can do.")


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip()
    if not query:
        return
    results = search_stocks_by_prefix(query)
    if not results:
        return
    await update.inline_query.answer(
        [
            InlineQueryResultArticle(
                id=s["ticker"],
                title=s["ticker"],
                description=s.get("sector") or "",
                input_message_content=InputTextMessageContent(f"/add {s['ticker']}"),
            )
            for s in results
        ],
        cache_time=60,
    )


async def on_error(update, context):
    log.error("[BOT] unhandled error: %s", context.error, exc_info=context.error)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("[BOT] starting — %d authorised user(s), max watchlist %d",
             len(AUTHORIZED_USERNAMES), MAX_WATCHLIST)

    # concurrent_updates: PTB processes updates sequentially by default, so one
    # user's Supabase round-trip blocks everyone else's command behind it.
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .concurrent_updates(True)
           .build())

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   handle_text_message))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_error_handler(on_error)

    # run_polling() is SYNCHRONOUS in python-telegram-bot v20+ — it builds and
    # owns the event loop itself. Awaiting it inside asyncio.run() raises
    # RuntimeError and kills the process on startup.
    #
    # Keep this service at 1 REPLICA. Telegram hands each update to whichever
    # poller asks first, so a second replica silently eats half the commands.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
