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
import logging.handlers
import os
import re
import sqlite3
import traceback

from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import NetworkError
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("card-bot")

DB_PATH = os.environ.get("CARD_BOT_DB", os.path.join(BASE_DIR, "cards.db"))
MAX_CARDS = 50

ASK_NUMBER, ASK_NAME = range(2)


# ---------- logging ----------
#
# logs/bot.log     everything INFO and up (what the bot did)
# logs/errors.log  ERROR and up only (what needs fixing) — read by /fix-errors
# Both rotate at 5 MB x 5 files. Card numbers and the bot token are masked in every line.

LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
ERROR_END = "=" * 80  # closes every error entry in the log files

_SECRETS: set[str] = set()
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
# A digit run, optionally split by single spaces or dashes ("4111 1111-1111 1111").
_DIGITS_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d)*")


def add_secret(value: str) -> None:
    """Never let this exact string (e.g. the bot token) reach a log line."""
    if value:
        _SECRETS.add(value)


def _mask_digit_run(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    if not 13 <= len(digits) <= 19:
        return m.group()
    # Supergroup/channel ids look like -100xxxxxxxxxx: keep them readable.
    if len(digits) == 13 and digits.startswith("100") and m.string[m.start() - 1:m.start()] == "-":
        return m.group()
    return f"••••{digits[-4:]}"


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "<bot-token>")
    text = _TOKEN_RE.sub("<bot-token>", text)
    return _DIGITS_RE.sub(_mask_digit_run, text)


class RedactingFormatter(logging.Formatter):
    """Masks tokens and card numbers in the finished line, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: str | None = None, console: bool = True) -> str:
    log_dir = log_dir or os.environ.get("CARD_BOT_LOG_DIR") or os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    formatter = RedactingFormatter(LOG_FORMAT, LOG_DATEFMT)

    def rotating(name: str, level: int) -> logging.Handler:
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, name), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setLevel(level)
        return handler

    handlers = [rotating("bot.log", logging.INFO), rotating("errors.log", logging.ERROR)]
    if console:
        handlers.append(logging.StreamHandler())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_dir


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


def save_card(user_id: int, number: str, name: str) -> bool:
    """Save or rename a card. Returns True if it is a new card."""
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM cards WHERE user_id = ? AND number = ?", (user_id, number)
        ).fetchone()
        conn.execute(
            "INSERT INTO cards (user_id, number, name) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, number) DO UPDATE SET name = excluded.name",
            (user_id, number, name),
        )
    return exists is None


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
        log.info("/start via 'Add a card' link: user=%s", update.effective_user.id)
        return await add_start(update, context)
    log.info("/start: user=%s", update.effective_user.id)
    await update.message.reply_text(
        HELP.format(bot=context.bot.username), parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if len(list_cards(user_id)) >= MAX_CARDS:
        log.info("Card add refused: user=%s already has %d cards", user_id, MAX_CARDS)
        await update.message.reply_text(f"You already have {MAX_CARDS} cards. Delete one in /cards.")
        return ConversationHandler.END
    log.info("Card add started: user=%s", user_id)
    await update.message.reply_text("Send the card number (16 digits).")
    return ASK_NUMBER


async def add_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    number = re.sub(r"[\s-]", "", update.message.text or "")
    user_id = update.effective_user.id
    if not re.fullmatch(r"\d{16}", number):
        log.info("Card number rejected: user=%s reason=not 16 digits (got %d characters)", user_id, len(number))
        await update.message.reply_text("That isn't 16 digits. Try again, or /cancel.")
        return ASK_NUMBER
    if not luhn_ok(number):
        log.info("Card number rejected: user=%s reason=checksum failed", user_id)
        await update.message.reply_text("That number looks mistyped (checksum failed). Try again, or /cancel.")
        return ASK_NUMBER
    context.user_data["number"] = number
    log.info("Card number accepted, waiting for name: user=%s card=%s", user_id, masked(number))
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
    user_id = update.effective_user.id
    if save_card(user_id, number, name):
        log.info("Card saved: user=%s card=%s name=%r", user_id, masked(number), name)
    else:
        log.info("Card renamed: user=%s card=%s new name=%r", user_id, masked(number), name)
    await update.message.reply_text(
        f"✅ Saved <b>{html.escape(name)}</b> ({masked(number)}).\n\n"
        f"Type <code>@{context.bot.username}</code> in any chat to share it.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("number", None)
    log.info("Card add cancelled: user=%s", update.effective_user.id)
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
    count = len(markup.inline_keyboard) if markup else 0
    log.info("/cards viewed: user=%s cards=%d", update.effective_user.id, count)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    # Buttons only exist in the private /cards list; refuse anything else.
    if query.message is None or query.message.chat.type != ChatType.PRIVATE:
        log.info("Card delete refused outside private chat: user=%s", query.from_user.id)
        await query.answer("🔒 Cards can only be managed in a private chat with me.", show_alert=True)
        return
    card_id = int(query.data.split(":", 1)[1])
    deleted = delete_card(query.from_user.id, card_id)
    if deleted:
        log.info("Card deleted: user=%s card_id=%s", query.from_user.id, card_id)
    else:
        log.info("Card delete: not found (already gone): user=%s card_id=%s", query.from_user.id, card_id)
    await query.answer("Deleted" if deleted else "Already gone")
    text, markup = cards_view(query.from_user.id)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ---------- groups: point to the private chat ----------

GROUP_REPLIES = {
    "add": "🔒 Cards can only be added in a private chat with me.",
    "cards": "🔒 Your cards are only shown in a private chat with me.",
    "start": "🔒 Cards can only be managed in a private chat with me.",
}


async def private_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # "/add@sendmycardbot extra" -> "add". Never reads or echoes anything else.
    command = update.message.text.split()[0].split("@")[0].lstrip("/").lower()
    payload = "add" if command == "add" else ""
    url = f"https://t.me/{context.bot.username}?start={payload}"
    chat = update.effective_chat
    log.info("Group redirect: chat=%s(%s) user=%s command=/%s",
             chat.id, chat.type, update.effective_user.id, command)
    await update.message.reply_text(
        GROUP_REPLIES.get(command, GROUP_REPLIES["start"]),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[Btn("💬 Open private chat", url=url)]]),
    )


# ---------- inline mode: @bot in any chat ----------

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
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
    log.info("Inline query: user=%s query_length=%d results=%d",
             query.from_user.id, len(query.query), len(results))


# ---------- errors ----------

def _mask_digits(text: str) -> str:
    return re.sub(r"\d", "#", text)


def describe_update(update: object) -> tuple[str, str]:
    """(short kind for the header, one-line summary without user content)."""
    if not isinstance(update, Update):
        return "no update (background/polling)", "none"
    chat, user = update.effective_chat, update.effective_user
    where = f"user={user.id if user else '-'} chat={chat.id if chat else '-'}({chat.type if chat else '-'})"
    if update.callback_query:
        return (f"callback query from {where}",
                f"callback_query data={_mask_digits(update.callback_query.data or '')!r}")
    if update.inline_query:
        return (f"inline query from {where}",
                f"inline_query query={_mask_digits(update.inline_query.query)!r}")
    message = update.effective_message
    if message:
        text = message.text or ""
        if text.startswith("/"):
            parts = text.split()
            content = f"command={parts[0]!r} args={len(parts) - 1}"
        elif text:
            content = f"text ({len(text)} characters, not logged)"
        else:
            content = "non-text message"
        return f"message from {where}", f"message {content}"
    return f"update from {where}", "other update type"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    kind, summary = describe_update(update)
    headline = f"{type(error).__name__}: {error}"
    if isinstance(error, NetworkError):
        # Includes TimedOut. Telegram/network hiccups are transient: python-telegram-bot retries.
        log.warning("Network problem while handling %s: %s (will retry)", kind, headline)
        return

    trace = "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
    log.error(
        "ERROR while handling %s: %s\n%s\nUpdate: %s\n%s",
        kind, headline, trace, summary, ERROR_END,
    )

    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE \
            and (update.message or update.callback_query):
        try:
            await context.bot.send_message(update.effective_chat.id, "⚠️ Something went wrong. Please try again.")
        except Exception as exc:  # the error handler itself must never crash
            log.warning("Could not tell the user about the error: %s: %s", type(exc).__name__, exc)


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


async def on_startup(app: Application) -> None:
    log.info("Bot started: @%s (id=%s)", app.bot.username, app.bot.id)


def build_app(token: str) -> Application:
    add_secret(token)
    app = Application.builder().token(token).post_init(on_startup).build()
    # Saving, listing and deleting cards happens only in the bot's private chat.
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
        fallbacks=[CommandHandler("cancel", cancel, filters=private)],
    ))
    app.add_handler(CommandHandler("cards", cards_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(delete_cb, pattern=r"^del:\d+$"))
    # In groups these commands only get a pointer to the private chat.
    app.add_handler(CommandHandler(
        ["start", "add", "cards"], private_only,
        filters=filters.ChatType.GROUPS & filters.UpdateType.MESSAGE,
    ))
    # Inline mode works in every chat: it only shares already-saved cards.
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    log_dir = setup_logging()
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set (put it in .env). Exiting.")
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    add_secret(token)
    init_db()
    log.info("Starting: database=%s logs=%s", DB_PATH, log_dir)

    app = build_app(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Bot stopped")


if __name__ == "__main__":
    main()
