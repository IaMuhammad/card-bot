# card-bot

Telegram bot (@sendmycardbot) that saves your bank cards (number + name only) in a private chat
and shares them in any chat via inline mode.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # put TELEGRAM_BOT_TOKEN=... in it
```

In @BotFather: `/setinline` -> choose the bot -> set a placeholder (e.g. "Pick a card").
Without it, typing `@sendmycardbot` shows nothing.

## Run

```bash
.venv/bin/python bot.py
```

Tests (offline, no token needed):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Logs

| File | Contains |
| --- | --- |
| `logs/bot.log` | Everything the bot does (INFO and up) |
| `logs/errors.log` | Only errors: header, traceback, update summary, `====` separator |
| `logs/errors.handled.log` | Errors already handled by `/fix-errors` |

Files rotate at 5 MB (5 backups). Card numbers are masked (`••••1234`) and the bot token is never written.
Set `CARD_BOT_LOG_DIR` to log somewhere else.

## Fix errors with Claude (on the server)

One command: Claude reads `logs/errors.log`, fixes the code with tests, archives the errors,
commits, and restarts the bot (`.claude/commands/fix-errors.md`):

```bash
cd /path/to/card-bot && claude -p "/fix-errors"
```

Restarting uses `sudo systemctl restart card-bot` if that systemd unit exists. Otherwise Claude
tells you to restart the bot yourself.
