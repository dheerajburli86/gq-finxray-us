import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = "8219968998"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Forward any user message to admin as a support ticket ─────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text or "[non-text message]"
    time_str = datetime.now().strftime("%d %b %Y · %I:%M %p EST")

    ticket = (
        f"📩 *New User Message — GQ FinXray US*\n\n"
        f"👤 *From:* {user.full_name} (@{user.username or 'no username'})\n"
        f"🆔 *Chat ID:* `{chat_id}`\n"
        f"🕐 *Time:* {time_str}\n\n"
        f"💬 *Message:*\n{text}"
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=ticket,
            parse_mode="Markdown"
        )
        logger.info(f"[TICKET] Forwarded message from {user.full_name} ({chat_id})")
    except Exception as e:
        logger.error(f"[ERROR] Failed to forward ticket: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    print("[TELEGRAM BOT] GQ FinXray US bot running — forwarding user messages to admin.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
