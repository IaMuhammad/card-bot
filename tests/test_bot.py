"""Offline tests: no network, no real token, never touches cards.db or logs/.

Run from the project root:
    .venv/bin/python -m unittest discover -s tests -v
"""
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="card-bot-test-")
os.environ["CARD_BOT_DB"] = os.path.join(TMP, "test.db")
os.environ["CARD_BOT_LOG_DIR"] = os.path.join(TMP, "logs")
sys.path.insert(0, ROOT)
import bot  # noqa: E402

LOG_DIR = bot.setup_logging(console=False)
assert bot.DB_PATH != os.path.join(ROOT, "cards.db")
assert os.path.abspath(LOG_DIR) != os.path.join(ROOT, "logs")

from telegram import Update  # noqa: E402
from telegram.ext import CommandHandler, ConversationHandler, ExtBot  # noqa: E402

USER = {"id": 42, "is_bot": False, "first_name": "U"}
CARD = "4111111111111111"
_uid = iter(range(1, 10_000))


def chat(kind):
    return {"id": 42, "type": "private"} if kind == "private" else {"id": -100123, "type": kind, "title": "G"}


def msg_update(app, text, kind):
    ents = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
    data = {"update_id": next(_uid), "message": {"message_id": next(_uid), "date": 0, "chat": chat(kind),
            "from": USER, "text": text, "entities": ents}}
    return Update.de_json(data, app.bot)


def cb_update(app, card_id, kind):
    data = {"update_id": next(_uid), "callback_query": {"id": "cb1", "from": USER, "chat_instance": "x",
            "data": f"del:{card_id}", "message": {"message_id": 7, "date": 0, "chat": chat(kind), "from": USER, "text": "cards"}}}
    return Update.de_json(data, app.bot)


def read_log(name):
    for handler in logging.getLogger().handlers:
        handler.flush()
    path = os.path.join(LOG_DIR, name)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def clear_logs():
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler) and handler.stream:
            handler.stream.seek(0)
            handler.stream.truncate()


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.init_db()
        with bot.db() as c:
            c.execute("DELETE FROM cards")
        clear_logs()

    async def asyncSetUp(self):
        self.patches = [
            patch.object(ExtBot, "username", new_callable=PropertyMock, return_value="sendmycardbot"),
            patch.object(ExtBot, "send_message", new_callable=AsyncMock),
            patch.object(ExtBot, "answer_callback_query", new_callable=AsyncMock),
            patch.object(ExtBot, "edit_message_text", new_callable=AsyncMock),
            patch.object(ExtBot, "answer_inline_query", new_callable=AsyncMock),
        ]
        mocks = [p.start() for p in self.patches]
        self.send, self.answer, self.edit = mocks[1], mocks[2], mocks[3]
        self.app = bot.build_app("123:abc")
        self.app._initialized = True  # skip initialize(): it would call getMe over the network
        self.conv = next(h for h in self.app.handlers[0] if isinstance(h, ConversationHandler))

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()

    async def run_update(self, upd):
        await self.app.process_update(upd)

    def conv_state(self, kind):
        return self.conv._conversations.get((chat(kind)["id"], USER["id"]))


class PrivateOnlyTest(BotTestCase):
    async def test_add_in_group_redirects_and_no_conversation(self):
        for kind in ("group", "supergroup"):
            for text, payload in (("/add", "add"), ("/add@sendmycardbot", "add"), ("/cards", ""), ("/start", "")):
                self.send.reset_mock()
                upd = msg_update(self.app, text, kind)
                self.assertFalse(self.conv.check_update(upd), f"{text} in {kind} must not hit conversation")
                await self.run_update(upd)
                self.send.assert_awaited_once()
                kw = self.send.await_args.kwargs
                self.assertTrue(kw["text"].startswith("🔒"))
                url = kw["reply_markup"].inline_keyboard[0][0].url
                self.assertEqual(url, f"https://t.me/sendmycardbot?start={payload}")
                self.assertIsNone(self.conv_state(kind))

    async def test_card_number_pasted_in_group_not_saved(self):
        await self.run_update(msg_update(self.app, "/add", "group"))
        await self.run_update(msg_update(self.app, "4111 1111 1111 1111", "group"))
        await self.run_update(msg_update(self.app, "My name", "group"))
        self.assertEqual(bot.list_cards(42), [])
        self.assertEqual(self.send.await_count, 1)  # only the redirect; nothing echoed

    async def test_add_in_private_enters_conversation_and_saves(self):
        upd = msg_update(self.app, "/add", "private")
        self.assertTrue(self.conv.check_update(upd))
        await self.run_update(upd)
        self.assertEqual(self.conv_state("private"), bot.ASK_NUMBER)
        await self.run_update(msg_update(self.app, "4111 1111 1111 1111", "private"))
        self.assertEqual(self.conv_state("private"), bot.ASK_NAME)
        await self.run_update(msg_update(self.app, "Test card", "private"))
        self.assertIsNone(self.conv_state("private"))
        self.assertEqual([c["name"] for c in bot.list_cards(42)], ["Test card"])
        await self.run_update(msg_update(self.app, "/add", "private"))
        await self.run_update(msg_update(self.app, "/cancel", "private"))
        self.assertIsNone(self.conv_state("private"))
        self.assertEqual(self.send.await_args.kwargs["text"], "Cancelled.")
        await self.run_update(msg_update(self.app, "/start add", "private"))
        self.assertEqual(self.conv_state("private"), bot.ASK_NUMBER)

    async def test_delete_callback_from_group_rejected(self):
        bot.save_card(42, CARD, "Keep me")
        card_id = bot.list_cards(42)[0]["id"]
        await self.run_update(cb_update(self.app, card_id, "supergroup"))
        self.answer.assert_awaited_once()
        self.assertTrue(self.answer.await_args.kwargs.get("show_alert"))
        self.edit.assert_not_awaited()
        self.assertEqual(len(bot.list_cards(42)), 1)
        await self.run_update(cb_update(self.app, card_id, "private"))
        self.assertEqual(bot.list_cards(42), [])
        self.edit.assert_awaited_once()


class RedactionTest(unittest.TestCase):
    def format(self, msg, *args, exc_info=None):
        record = logging.LogRecord("card-bot", logging.ERROR, __file__, 1, msg, args, exc_info)
        return bot.RedactingFormatter(bot.LOG_FORMAT, bot.LOG_DATEFMT).format(record)

    def test_token_redacted(self):
        token = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
        out = self.format("GET https://api.telegram.org/bot%s/getUpdates failed", token)
        self.assertNotIn(token, out)
        self.assertNotIn("AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ", out)
        self.assertIn("<bot-token>", out)
        bot.add_secret("short-secret-xyz")
        self.assertNotIn("short-secret-xyz", self.format("value=%s", "short-secret-xyz"))

    def test_card_numbers_masked_in_all_spellings(self):
        for spelling in ("4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111", "8600123412341234567"):
            out = self.format("user typed %s here", spelling)
            self.assertNotIn(spelling, out)
            self.assertNotIn("41111111", out.replace(" ", "").replace("-", ""))
            self.assertIn("••••" + spelling[-4:], out)

    def test_ordinary_numbers_kept(self):
        out = self.format("user=6238676402 chat=-1001234567890 card_id=12 at 2026-09-15 23:10:04")
        self.assertIn("user=6238676402", out)
        self.assertIn("chat=-1001234567890", out)
        self.assertIn("2026-09-15 23:10:04", out)

    def test_traceback_redacted(self):
        try:
            raise ValueError(f"bad card {CARD} with token 7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ")
        except ValueError:
            out = self.format("boom", exc_info=sys.exc_info())
        self.assertIn("Traceback (most recent call last)", out)
        self.assertNotIn(CARD, out)
        self.assertIn("••••1111", out)
        self.assertIn("<bot-token>", out)


class ErrorLoggingTest(BotTestCase):
    async def test_handler_exception_goes_to_errors_log_and_user_is_told(self):
        async def broken(update, context):
            raise RuntimeError(f"database exploded for {CARD}")

        self.app.add_handler(CommandHandler("boom", broken), group=-1)
        await self.run_update(msg_update(self.app, "/boom 4111111111111111", "private"))

        errors = read_log("errors.log")
        self.assertIn("| ERROR | card-bot | ERROR while handling message from user=42 chat=42(private): "
                      "RuntimeError: database exploded for ••••1111", errors)
        self.assertIn("Traceback (most recent call last)", errors)
        self.assertIn("in broken", errors)
        self.assertIn("Update: message command='/boom' args=1", errors)
        self.assertIn(bot.ERROR_END, errors)
        self.assertNotIn(CARD, errors)
        self.assertIn("ERROR while handling", read_log("bot.log"))

        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.args, (42, "⚠️ Something went wrong. Please try again."))

    async def test_error_reply_failure_does_not_crash_handler(self):
        async def broken(update, context):
            raise RuntimeError("boom")

        self.send.side_effect = RuntimeError("telegram down")
        self.app.add_handler(CommandHandler("boom", broken), group=-1)
        await self.run_update(msg_update(self.app, "/boom", "private"))
        self.assertIn("RuntimeError: boom", read_log("errors.log"))
        self.assertIn("Could not tell the user about the error", read_log("bot.log"))

    async def test_network_error_is_warning_not_error(self):
        from telegram.error import TimedOut
        context = type("Ctx", (), {"error": TimedOut(), "bot": self.app.bot})()
        await bot.on_error(None, context)
        self.assertEqual(read_log("errors.log"), "")
        self.assertIn("| WARNING | card-bot | Network problem", read_log("bot.log"))

    async def test_normal_events_in_bot_log_only(self):
        await self.run_update(msg_update(self.app, "/add", "private"))
        await self.run_update(msg_update(self.app, "1234", "private"))
        await self.run_update(msg_update(self.app, "4111 1111 1111 1111", "private"))
        await self.run_update(msg_update(self.app, "My Visa", "private"))
        await self.run_update(msg_update(self.app, "/cards", "private"))
        await self.run_update(msg_update(self.app, "/cards", "group"))
        card_id = bot.list_cards(42)[0]["id"]
        await self.run_update(cb_update(self.app, card_id, "private"))
        await self.run_update(Update.de_json({"update_id": next(_uid), "inline_query": {
            "id": "iq1", "from": USER, "query": "vis", "offset": ""}}, self.app.bot))

        log_text = read_log("bot.log")
        for expected in (
            "Card add started: user=42",
            "Card number rejected: user=42 reason=not 16 digits",
            "Card saved: user=42 card=Visa •••• 1111 name='My Visa'",
            "/cards viewed: user=42 cards=1",
            "Group redirect: chat=-100123(group) user=42 command=/cards",
            f"Card deleted: user=42 card_id={card_id}",
            "Inline query: user=42 query_length=3 results=0",
        ):
            self.assertIn(expected, log_text)
        self.assertNotIn(CARD, log_text)
        self.assertNotIn("4111 1111 1111 1111", log_text)
        self.assertEqual(read_log("errors.log"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
