# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**openlucky** is a lightweight Telegram-controlled Claude Code daemon. A long-running daemon receives messages from a Telegram bot, dispatches them to Claude Code as the execution engine, and returns a summary of the result back to the user via the same Telegram chat.

Language: **Python**

## Running the Service

**Production** (systemd):
```bash
sudo cp openlucky.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable openlucky
sudo systemctl start openlucky
sudo journalctl -u openlucky -f   # follow logs
```

**Dev** (foreground):
```bash
source .venv/bin/activate
CONFIG_FILE=config/settings.dev.yaml python3 -m app.main
```

## Dev vs Prod

Two separate bots and data directories to avoid conflicts:

| | Prod | Dev |
|---|---|---|
| Config | `config/settings.yaml` | `config/settings.dev.yaml` |
| Data | `data/` | `data-dev/` |
| Bot | prod bot token | dev bot token |

`CONFIG_FILE` env var selects the config. `data_dir` in the yaml controls where DB and logs are stored. Both files are gitignored — use the `.example` files as templates.

## Architecture

```
Telegram User
     │
     ▼
Telegram Bot (long-polling)
     │
     ▼
┌─────────────────────────────────────────┐
│  Daemon                                 │
│  command_router  →  handle /commands    │
│  session_manager →  new vs resume       │
│  claude_runner   →  subprocess + output │
│  db (SQLite)     →  state persistence   │
└─────────────────────────────────────────┘
     │
     ▼
Claude Code CLI (execution engine)
     │
     ▼
Summary sent back → Telegram
```

### Three-layer abstraction (critical distinction)
- **session** — Claude Code's task context (`--resume <session_id>`)
- **job** — one execution triggered by one user message
- **process** — the local subprocess carrying that job

## Project Structure

```
app/
  main.py             # entry point, starts bot + daemon
  telegram_bot.py     # Telegram polling, message entry point
  command_router.py   # identifies and handles /commands
  session_manager.py  # new vs resume decision, reads/writes chats table
  claude_runner.py    # subprocess management only
  daemon.py           # job lifecycle orchestration
  db.py               # SQLite init and CRUD
  models.py           # dataclasses: Job, ChatState, RunResult
  config.py           # loads settings.yaml, respects CONFIG_FILE env var
  formatter.py        # Telegram message formatting
config/
  settings.yaml           # prod config (gitignored)
  settings.yaml.example   # template
  settings.dev.yaml       # dev config (gitignored)
  settings.dev.yaml.example
data/                 # prod runtime state (gitignored)
data-dev/             # dev runtime state (gitignored)
openlucky.service     # systemd unit file
```

## Claude Code Integration

`claude_runner.py` has exactly one responsibility: build the command, spawn the subprocess, collect output, parse `session_id`. It knows nothing about Telegram or the database.

Invoke Claude Code with:
```
claude -p "<prompt>" --output-format stream-json --verbose
claude -p "<prompt>" --output-format stream-json --verbose --resume <session_id>
```

`--verbose` is required when combining `-p` with `--output-format stream-json`, otherwise Claude exits with code 1.

**Important:** `claude_bin` in settings must be an absolute path (e.g. `/home/user/.local/bin/claude`). systemd runs with a minimal PATH and won't find `claude` by name alone.

Session ID is parsed from the `{"type": "result", "session_id": "..."}` line in stdout.

## Command Protocol

Control commands (handled by `command_router`, never sent to Claude Code):

| Command | Behavior |
|---|---|
| `/status` | Current status, task name, cwd, last job time |
| `/stop` | Terminate current subprocess → job=canceled, chat=idle |
| `/new` | Force next message to open a new session |
| `/reset` | Clear active_session_id binding (history kept) |
| `/cwd /path` | Switch working directory, force new session |
| `/task name` | Set active task name |

## Session Decision Logic

Resume current session when **all** conditions are met:
- `active_session_id` exists
- Last activity < 30 minutes ago
- No `/new` flag set
- Message looks like a follow-up (keywords: 继续, 刚才, 再试, continue, fix this too, run again, etc.)

Otherwise: new session.

## Dev Tooling

```bash
pip install -r requirements-dev.txt
pytest                    # run all tests
pytest tests/test_db.py   # run single test file
ruff check app/ tests/    # lint
ruff format app/ tests/   # format
mypy app/                 # type check
```

## Debugging

Raw output (stdout + stderr) for every job is saved to `data/jobs/<job_id>.log`. Check there first when exit code != 0.

## MVP Constraints (intentional scope limits)

Do NOT add in v1:
- Multi-user permission system
- True streaming token-by-token forwarding
- Parallel sessions
- Complex memory/summarization
- Auto repo discovery
- Async worker pool (a background thread/task per job is sufficient for single-user)
