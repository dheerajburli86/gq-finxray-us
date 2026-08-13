"""
telegram_bot.py
GQ FinXray US — watchlist management bot.

Replaces the previous file, which was a 50-line support-ticket forwarder with no
commands and no database access. This one is the registration and watchlist
surface the delivery layer depends on: if a user is not in `users` with a
chat_id, and their tickers are not in `watchlists`, they receive nothing.

Storage is Supabase, not memory. The earlier in-memory version lost every
watchlist when the process restarted, and — more importantly — the alert
delivery loop runs in a different process and could never see it.

Identity: keyed on telegram_chat_id, which is stable for a given (user, bot)
pair. Usernames are stored for display but are NOT the key, because a user can
change their Telegram username at any time and we would lose them.

Commands
    /start              register (or re-activate) and bind this chat
    /add AAPL MSFT      add one or more tickers, validated against `stocks`
    /remove AAPL        remove a ticker
    /list               show the current watchlist
    /settings           show current preferences
    /settings impact high|medium|low
    /settings market on|off
    /stop               pause all alerts (keeps the watchlist)
    /help
"""

import os
import logging

from dotenv import load_dotenv
from supabase import create_client
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Caps the per-user API cost and keeps the watchlist heatmap readable.
MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST_SIZE", "50"))

IMPACT_CHOICES = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


# ── User + watchlist persistence ──────────────────────────────────────────────
def get_or_create_user(chat_id, username=None, user_id=None, display_name=None,
                       reactivate=False):
    """Idempotent registration keyed on chat_id. Returns the user row, or None.

    `reactivate` must be True ONLY for /start. Every command handler calls this
    function, and it used to force is_active=True unconditionally — so a user who
    sent /stop was silently resubscribed the moment they sent /list or /settings
    to check that the stop had worked. Opting out has to survive the user looking
    at their own settings.
    """
    chat_id = str(chat_id)
    try:
        found = (supabase.table("users").select("*")
                 .eq("telegram_chat_id", chat_id).limit(1).execute()).data
        if found:
            user = found[0]
            patch = {"is_active": True} if reactivate else {}
            if username and user.get("telegram_username") != username:
                patch["telegram_username"] = username
            if display_name and user.get("display_name") != display_name:
                patch["display_name"] = display_name
            if user_id and user.get("telegram_user_id") != str(user_id):
                patch["telegram_user_id"] = str(user_id)
            # An empty patch is now possible (a returning user with no changed
            # profile fields and no reactivation); PostgREST rejects an empty
            # UPDATE body, so only write when there is something to write.
            if patch:
                supabase.table("users").update(patch).eq("id", user["id"]).execute()
                user.update(patch)
            _ensure_prefs(user["id"])
            return user

        created = (supabase.table("users").insert({
            "telegram_chat_id": chat_id,
            "telegram_username": username,
            "telegram_user_id": str(user_id) if user_id else None,
            "display_name": display_name,
            "is_active": True,
        }).execute()).data
        if created:
            _ensure_prefs(created[0]["id"])
            return created[0]
    except Exception as e:
        logger.error("[BOT] get_or_create_user failed for chat %s: %s", chat_id, e)
    return None


def _ensure_prefs(user_id):
    try:
        supabase.table("user_preferences").upsert(
            {"user_id": user_id}, on_conflict="user_id", ignore_duplicates=True
        ).execute()
    except Exception as e:
        logger.error("[BOT] Failed to ensure preferences for %s: %s", user_id, e)


def get_prefs(user_id):
    try:
        rows = (supabase.table("user_preferences").select("*")
                .eq("user_id", user_id).limit(1).execute()).data
        if rows:
            return rows[0]
    except Exception as e:
        logger.error("[BOT] get_prefs failed: %s", e)
    return {"min_impact": "MEDIUM", "max_alerts_per_day": 200}


def lookup_stock(ticker):
    """Validate against the 6,353-row stocks table. Returns the row or None."""
    try:
        rows = (supabase.table("stocks").select("ticker, exchange, sector")
                .eq("ticker", ticker.upper()).limit(1).execute()).data
        return rows[0] if rows else None
    except Exception as e:
        logger.error("[BOT] lookup_stock(%s) failed: %s", ticker, e)
        return None


def get_watchlist(user_id):
    try:
        rows = (supabase.table("watchlists").select("ticker, company_name")
                .eq("user_id", user_id).order("ticker").execute()).data or []
        return rows
    except Exception as e:
        logger.error("[BOT] get_watchlist failed: %s", e)
        return []


def add_ticker(user_id, ticker, company_name=None):
    """Returns 'added' | 'exists' | 'error'."""
    ticker = ticker.upper()
    try:
        # There is no UNIQUE(user_id, ticker) constraint on watchlists, so the
        # duplicate branch below could never fire and `/add AAPL` twice inserted
        # two rows. Delivery joins per row, so the user then received every AAPL
        # alert twice. Check before inserting.
        existing = (supabase.table("watchlists").select("id")
                    .eq("user_id", user_id).eq("ticker", ticker)
                    .limit(1).execute()).data
        if existing:
            return "exists"

        supabase.table("watchlists").insert({
            "user_id": user_id, "ticker": ticker, "company_name": company_name,
        }).execute()
        return "added"
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            return "exists"
        logger.error("[BOT] add_ticker failed: %s", e)
        return "error"


def remove_ticker(user_id, ticker):
    try:
        res = (supabase.table("watchlists").delete()
               .eq("user_id", user_id).eq("ticker", ticker.upper()).execute())
        return bool(res.data)
    except Exception as e:
        logger.error("[BOT] remove_ticker failed: %s", e)
        return False


# ── Handlers ──────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    user = get_or_create_user(
        chat_id=update.effective_chat.id,
        username=tg.username,
        user_id=tg.id,
        display_name=tg.full_name,
        reactivate=True,   # /start is the ONLY command that resubscribes.
    )
    if not user:
        await update.message.reply_text("Couldn't set up your account just now. Try /start again in a moment.")
        return

    count = len(get_watchlist(user["id"]))
    if count:
        await update.message.reply_text(
            f"Welcome back{', ' + tg.first_name if tg.first_name else ''}. "
            f"You're tracking <b>{count}</b> stock{'s' if count != 1 else ''} and alerts are on.\n\n"
            "<code>/list</code> to see them · <code>/add TICKER</code> to add more",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"Welcome to <b>GQ FinXray US</b>{', ' + tg.first_name if tg.first_name else ''}.\n\n"
        "You'll get alerts only for the stocks you add — filings, news, earnings, "
        "insider trades, analyst moves and technical signals.\n\n"
        "Add your first stock to begin:\n"
        "<code>/add TICKER</code>   (e.g. <code>/add TSLA</code>)\n\n"
        "You can add several at once: <code>/add TSLA NVDA AMD</code>\n\n"
        "<code>/list</code> — see your stocks\n"
        "<code>/remove TICKER</code> — stop tracking one\n"
        "<code>/settings</code> — alert preferences\n"
        "<code>/help</code> — all commands",
        parse_mode=ParseMode.HTML,
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    user = get_or_create_user(update.effective_chat.id, tg.username, tg.id, tg.full_name)
    if not user:
        await update.message.reply_text("Something went wrong. Try /start first.")
        return

    if not context.args:
        await update.message.reply_text(
            "Tell me which stock:\n<code>/add TICKER</code>\n\n"
            "You can add several at once — <code>/add TICKER TICKER TICKER</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    current = len(get_watchlist(user["id"]))
    room = MAX_WATCHLIST - current
    if room <= 0:
        await update.message.reply_text(
            f"Your watchlist is full ({MAX_WATCHLIST} stocks). "
            "Remove one with <code>/remove TICKER</code> to make space.",
            parse_mode=ParseMode.HTML,
        )
        return

    added, existed, unknown = [], [], []
    for raw in context.args[:room + 5]:
        t = raw.strip().upper().lstrip("$")
        if not t or not t.replace(".", "").replace("-", "").isalnum():
            unknown.append(raw)
            continue
        if len(added) >= room:
            break
        stock = lookup_stock(t)
        if not stock:
            unknown.append(t)
            continue
        result = add_ticker(user["id"], t, stock.get("sector"))
        if result == "added":
            added.append(t)
        elif result == "exists":
            existed.append(t)
        else:
            unknown.append(t)

    parts = []
    if added:
        parts.append(f"✅ Added <b>{', '.join(added)}</b>")
    if existed:
        parts.append(f"Already tracking {', '.join(existed)}")
    if unknown:
        parts.append(f"❌ Not found on NYSE or NASDAQ: {', '.join(unknown)}")
    if added and (current + len(added)) >= MAX_WATCHLIST:
        parts.append(f"That's your {MAX_WATCHLIST}-stock limit.")

    await update.message.reply_text("\n".join(parts) or "Nothing to add.", parse_mode=ParseMode.HTML)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    user = get_or_create_user(update.effective_chat.id, tg.username, tg.id, tg.full_name)
    if not user:
        await update.message.reply_text("Something went wrong. Try /start first.")
        return

    if not context.args:
        await update.message.reply_text(
            "Which one?\n<code>/remove TICKER</code>", parse_mode=ParseMode.HTML)
        return

    removed, missing = [], []
    for raw in context.args:
        t = raw.strip().upper().lstrip("$")
        (removed if remove_ticker(user["id"], t) else missing).append(t)

    parts = []
    if removed:
        parts.append(f"✅ Removed <b>{', '.join(removed)}</b>")
    if missing:
        parts.append(f"Not on your watchlist: {', '.join(missing)}")
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    user = get_or_create_user(update.effective_chat.id, tg.username, tg.id, tg.full_name)
    if not user:
        await update.message.reply_text("Something went wrong. Try /start first.")
        return

    rows = get_watchlist(user["id"])
    if not rows:
        await update.message.reply_text(
            "Your watchlist is empty, so you're not receiving any alerts yet.\n\n"
            "Add one with <code>/add TICKER</code>", parse_mode=ParseMode.HTML)
        return

    lines = [f"<b>Your watchlist — {len(rows)} stock{'s' if len(rows) != 1 else ''}</b>", ""]
    lines += [f"• <b>{r['ticker']}</b>" + (f"  <i>{r['company_name']}</i>" if r.get("company_name") else "")
              for r in rows]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    user = get_or_create_user(update.effective_chat.id, tg.username, tg.id, tg.full_name)
    if not user:
        await update.message.reply_text("Something went wrong. Try /start first.")
        return

    args = [a.lower() for a in (context.args or [])]

    if len(args) >= 2 and args[0] == "impact":
        choice = IMPACT_CHOICES.get(args[1])
        if not choice:
            await update.message.reply_text("Pick one: high, medium or low.")
            return
        supabase.table("user_preferences").update({"min_impact": choice}).eq("user_id", user["id"]).execute()
        note = {"HIGH": "Only the biggest moves.",
                "MEDIUM": "Meaningful news and above.",
                "LOW": "Everything, including routine filings."}[choice]
        await update.message.reply_text(f"✅ Minimum impact set to <b>{choice}</b>. {note}",
                                        parse_mode=ParseMode.HTML)
        return

    p = get_prefs(user["id"])
    await update.message.reply_text(
        "<b>Your settings</b>\n\n"
        f"Minimum impact: <b>{p.get('min_impact', 'MEDIUM')}</b>\n"
        f"Daily limit: <b>{p.get('max_alerts_per_day', 200)}</b> alerts\n\n"
        "Change them:\n"
        "<code>/settings impact high</code>\n"
        "<code>/settings impact medium</code>\n"
        "<code>/settings impact low</code>",
        parse_mode=ParseMode.HTML,
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    user = get_or_create_user(update.effective_chat.id, tg.username, tg.id, tg.full_name)
    if user:
        supabase.table("users").update({"is_active": False}).eq("id", user["id"]).execute()
    await update.message.reply_text(
        "Alerts paused. Your watchlist is saved — send /start whenever you want them back.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>GQ FinXray US</b>\n\n"
        "You receive alerts only for the stocks on your watchlist.\n\n"
        "<code>/add TICKER</code> — track a stock\n"
        "<code>/remove TICKER</code> — stop tracking it\n"
        "<code>/list</code> — your watchlist\n"
        "<code>/settings</code> — alert preferences\n"
        "<code>/stop</code> — pause all alerts\n\n"
        "<b>What you'll get</b>\n"
        "SEC filings (8-K, 10-Q, 10-K, Form 4) · company news · quarterly results · "
        "earnings calendar and call summaries · insider and large trades · analyst rating changes · "
        "technical signals · plus a daily heatmap of your watchlist.",
        parse_mode=ParseMode.HTML,
    )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A bare ticker like 'AAPL' is almost certainly an attempt to add it."""
    text = (update.message.text or "").strip().upper().lstrip("$")
    if 1 <= len(text) <= 5 and text.isalpha() and lookup_stock(text):
        await update.message.reply_text(
            f"Did you mean <code>/add {text}</code>?", parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text("Send /help to see what I can do.")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is not set")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler(["list", "watchlist"], list_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    logger.info("[BOT] Starting watchlist bot (max watchlist = %d)", MAX_WATCHLIST)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
