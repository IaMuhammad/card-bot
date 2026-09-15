#!/usr/bin/env python3
"""Telegram bot that saves your bank cards and shares them in any chat.

Flow:
    In the bot's private chat:  /add -> card number -> name  (saved)
    In any chat:                type @yourbot  -> pick a card -> it is sent

Only the card number and a name are stored — never the expiry date or CVV.

Setup:
    python3 -m venv .venv && . .venv/bin/activate
    pip install "python-telegram-bot>=21,<23"
    export TELEGRAM_BOT_TOKEN=123456:ABC...
    python bot.py

In @BotFather: /setinline -> choose the bot -> set a placeholder (e.g. "Pick a card").
Without this step, typing @yourbot shows nothing.
"""
import html
import logging
import os
import re
import sqlite3

from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("card-bot")

DB_PATH = os.environ.get("CARD_BOT_DB", os.path.join(os.path.dirname(__file__), "cards.db"))
MAX_CARDS = 50

ASK_NUMBER, ASK_NAME = range(2)


# ---------- storage ----------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cards (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL,
                   number TEXT NOT NULL,
                   name TEXT NOT NULL,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE (user_id, number)
               )"""
        )


def list_cards(user_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT id, number, name FROM cards WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()


def save_card(user_id: int, number: str, name: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO cards (user_id, number, name) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, number) DO UPDATE SET name = excluded.name",
            (user_id, number, name),
        )


def delete_card(user_id: int, card_id: int) -> bool:
    with db() as conn:
        return conn.execute(
            "DELETE FROM cards WHERE id = ? AND user_id = ?", (card_id, user_id)
        ).rowcount > 0


# ---------- card helpers ----------

def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def brand(number: str) -> str:
    if number.startswith(("8600", "5614")):
        return "Uzcard"
    if number.startswith("9860"):
        return "Humo"
    if number.startswith("4"):
        return "Visa"
    if number.startswith(("51", "52", "53", "54", "55", "22", "23", "24", "25", "26", "27")):
        return "Mastercard"
    if number.startswith("62"):
        return "UnionPay"
    return "Card"


def pretty(number: str) -> str:
    return " ".join(number[i:i + 4] for i in range(0, len(number), 4))


def masked(number: str) -> str:
    return f"{brand(number)} •••• {number[-4:]}"


# ---------- private chat: add / list / delete ----------

HELP = (
    "💳 <b>Card bot</b>\n\n"
    "/add — save a card\n"
    "/cards — your cards (delete here)\n"
    "/cancel — stop adding\n\n"
    "To share a card in any chat, type <code>@{bot}</code> and pick one.\n"
    "Only the number and a name are saved — never send expiry or CVV."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Opened from the "Add a card" button in the inline picker.
    if context.args and context.args[0] == "add":
        return await add_start(update, context)
    await update.message.reply_text(
        HELP.format(bot=context.bot.username), parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if len(list_cards(update.effective_user.id)) >= MAX_CARDS:
        await update.message.reply_text(f"You already have {MAX_CARDS} cards. Delete one in /cards.")
        return ConversationHandler.END
    await update.message.reply_text("Send the card number (16 digits).")
    return ASK_NUMBER


async def add_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    number = re.sub(r"[\s-]", "", update.message.text or "")
    if not re.fullmatch(r"\d{16}", number):
        await update.message.reply_text("That isn't 16 digits. Try again, or /cancel.")
        return ASK_NUMBER
    if not luhn_ok(number):
        await update.message.reply_text("That number looks mistyped (checksum failed). Try again, or /cancel.")
        return ASK_NUMBER
    context.user_data["number"] = number
    await update.message.reply_text(
        f"{masked(number)}\n\nNow send a name for it — e.g. <i>Muhammad A. — Kapitalbank</i>.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()[:64]
    if not name:
        await update.message.reply_text("Send a name as text, or /cancel.")
        return ASK_NAME
    number = context.user_data.pop("number")
    save_card(update.effective_user.id, number, name)
    await update.message.reply_text(
        f"✅ Saved <b>{html.escape(name)}</b> ({masked(number)}).\n\n"
        f"Type <code>@{context.bot.username}</code> in any chat to share it.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("number", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def cards_view(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    cards = list_cards(user_id)
    if not cards:
        return "You have no saved cards. Use /add.", None
    lines = [f"{i}. <b>{html.escape(c['name'])}</b>\n<code>{pretty(c['number'])}</code>"
             for i, c in enumerate(cards, 1)]
    buttons = [[Btn(f"🗑 {i}. {c['name'][:30]}", callback_data=f"del:{c['id']}")]
               for i, c in enumerate(cards, 1)]
    return "💳 Your cards:\n\n" + "\n\n".join(lines), InlineKeyboardMarkup(buttons)


async def cards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = cards_view(update.effective_user.id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    card_id = int(query.data.split(":", 1)[1])
    deleted = delete_card(query.from_user.id, card_id)
    await query.answer("Deleted" if deleted else "Already gone")
    text, markup = cards_view(query.from_user.id)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ---------- inline mode: @bot in any chat ----------

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    log.info("Inline query from %s: %r", query.from_user.id, query.query)
    term = query.query.strip().lower()
    cards = [c for c in list_cards(query.from_user.id)
             if not term or term in c["name"].lower() or term in c["number"]]

    results = [
        InlineQueryResultArticle(
            id=str(c["id"]),
            title=c["name"],
            description=f"{brand(c['number'])}  {pretty(c['number'])}",
            input_message_content=InputTextMessageContent(
                f"<code>{pretty(c['number'])}</code>\n{html.escape(c['name'])}",
                parse_mode=ParseMode.HTML,
            ),
        )
        for c in cards
    ]
    await query.answer(
        results,
        cache_time=0,        # show new/deleted cards immediately
        is_personal=True,    # never serve one user's cards to another
        button=InlineQueryResultsButton(text="➕ Add a card", start_parameter="add"),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error while handling %r", update, exc_info=context.error)


# ---------- main ----------

def load_dotenv(path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) -> None:
    """Read KEY=VALUE lines from .env; real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    init_db()

    app = Application.builder().token(token).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=private),
            CommandHandler("add", add_start, filters=private),
        ],
        states={
            ASK_NUMBER: [MessageHandler(private & filters.TEXT & ~filters.COMMAND, add_number)],
            ASK_NAME: [MessageHandler(private & filters.TEXT & ~filters.COMMAND, add_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(CommandHandler("cards", cards_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(delete_cb, pattern=r"^del:\d+$"))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_error_handler(on_error)

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
