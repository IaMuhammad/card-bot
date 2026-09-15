---
description: Read the bot's error log, fix the bugs with tests, archive the handled errors, and restart the bot
allowed-tools: Read, Edit, Write, Glob, Grep, Bash(ls:*), Bash(cat:*), Bash(tail:*), Bash(head:*), Bash(grep:*), Bash(wc:*), Bash(date:*), Bash(.venv/bin/python:*), Bash(git status:*), Bash(git diff:*), Bash(git add:*), Bash(git commit:*), Bash(git push:*), Bash(git rev-parse:*), Bash(git branch:*), Bash(systemctl status:*), Bash(systemctl list-unit-files:*), Bash(sudo systemctl restart card-bot), Bash(journalctl:*), Bash(pgrep:*), Bash(truncate:*)
---

You maintain the Telegram card bot in this directory (`bot.py`, tests in `tests/`, venv in `.venv`).
Your job: read the error log, fix what is broken, prove it with tests, then update and restart the bot.

## Hard rules
- Never print, log, or commit the bot token or `.env`. Do not read `.env`.
- Never log or print full card numbers. Never read, modify, or delete `cards.db` contents.
- Do not kill processes. Do not change code for errors that are not caused by the code.

## 1. Read the errors
- Read `logs/errors.log` and any rotated `logs/errors.log.1` … `logs/errors.log.5`.
- If they are all missing or empty: reply "No errors" and stop.
- Each entry starts with a line like
  `2026-09-15 23:10:04 | ERROR | card-bot | ERROR while handling message from user=… chat=…(private): KeyError: 'number'`
  followed by the traceback, an `Update: …` summary line, and ends with a line of `=` characters.
  Other ERROR/CRITICAL lines (from `telegram.ext…`) are single entries with their traceback.

## 2. Group by root cause
- Group entries by exception type + the deepest frame in `bot.py` (file:line / function).
- Order groups by count, then by most recent timestamp.
- For context, read `logs/bot.log` around each group's timestamps (what the user did just before).

## 3. Classify and fix each group
- **External** (do not change code, just report): `Conflict` (another bot instance is polling),
  `InvalidToken`/`Unauthorized` (bad token), `NetworkError`/`TimedOut`/`RetryAfter`, Telegram outages,
  disk full, permissions on the server.
- **Code bug**: find the cause in `bot.py`, make the smallest correct fix that matches the existing style.
  Add or update a test in `tests/test_bot.py` that reproduces the error first (feed an `Update.de_json(...)`
  through `app.process_update` like the existing tests), then make it pass.
- Run the full suite from the project root until it is green:
  `.venv/bin/python -m unittest discover -s tests -v`
- If you cannot find a safe fix, leave the code alone for that group and explain why in the report.

## 4. Archive handled errors
Only after the tests pass. Append the current errors to the archive, then empty the log so the same errors
are not fixed twice:
1. Append a line `=== handled <YYYY-MM-DD HH:MM:SS>: <one-line summary of groups and fixes> ===`
   to `logs/errors.handled.log`, followed by the full contents of `logs/errors.log` and the rotated files.
2. Truncate them: `truncate -s 0 logs/errors.log` (and delete nothing else). Remove rotated
   `errors.log.N` files only after their contents are in `errors.handled.log`.

If you fixed nothing because every error was external, still archive them (summary says "external: …").

## 5. Update the project
- If this is a git repo (`git rev-parse --is-inside-work-tree`): `git add` only the files you changed
  (code and tests — never `.env`, `cards.db`, `logs/`) and commit with a clear message such as
  `Fix KeyError in add_name when conversation state was lost`.
  Push only if the branch has an upstream (`git rev-parse --abbrev-ref @{u}` succeeds).
- Restart the bot:
  - If a systemd unit `card-bot` exists (`systemctl list-unit-files card-bot.service`):
    `sudo systemctl restart card-bot`, then check `systemctl status card-bot --no-pager` and
    `journalctl -u card-bot -n 30 --no-pager` for a clean start.
  - Otherwise, if a `bot.py` process is running (`pgrep -af bot.py`), do not kill it: tell the user it must be
    restarted to load the fix.
- Then `tail -n 20 logs/bot.log` and confirm a fresh `Bot started: @…` line (only expected if it was restarted).

## 6. Report (short)
For each error group: count and last seen, cause, fix (file:line) or "external — no code change",
test added. Then: test result (N passed), commit hash / push status, restart result.
