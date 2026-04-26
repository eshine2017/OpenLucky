# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**openlucky** is a lightweight Telegram-controlled Claude Code daemon. A long-running daemon receives messages from a Telegram bot, dispatches them to Claude Code as the execution engine, and returns a summary of the result back to the user via the same Telegram chat.

Language: **Python 3.12+**

## Quick Start

```bash
just venv          # create .venv and install dependencies
just dev           # start dev service (uses config/settings.dev.yaml)
just ci            # lint + typecheck + tests with coverage (mirrors CI)
```

Run `just --list` to see all available commands.

## Dev vs Prod

Two separate bots and data directories to avoid conflicts:

| | Prod | Dev |
|---|---|---|
| Config | `config/settings.yaml` | `config/settings.dev.yaml` |
| Data | `data/` | `data-dev/` |

`CONFIG_FILE` env var selects the config. Both config files are gitignored — use the `.example` files as templates.

## Architecture

```
Telegram User
     │
     ▼
TelegramBot (long-polling)
     │  dispatches messages
     ▼
Daemon ──► CommandRouter (handles /commands)
     │
     ▼
SessionManager (new vs resume decision)
     │
     ▼
ClaudeCodeAgent (subprocess wrapper)
     │
     ▼
Claude Code CLI  →  summary sent back to Telegram
```

### Three-layer abstraction (critical distinction)

- **session** — Claude Code's task context (`--resume <session_id>`)
- **job** — one execution triggered by one user message
- **process** — the local subprocess carrying that job

### Module responsibilities

| Module | Responsibility |
|---|---|
| `main.py` | Entry point — wires bot + daemon together |
| `telegram_bot.py` | PTB long-polling; hands messages to daemon |
| `daemon.py` | Job lifecycle orchestration; owns the event queue |
| `command_router.py` | Parses and executes `/commands`; never touches Claude |
| `session_manager.py` | Decides new vs resume; reads/writes `chats` table |
| `agents/claude_code.py` | Spawns subprocess, cancels it, parses stream-json output |
| `db.py` | SQLite init and CRUD |
| `models.py` | Dataclasses: `Job`, `ChatState`, `RunResult` |
| `config.py` | Loads settings.yaml; respects `CONFIG_FILE` env var |
| `formatter.py` | Formats messages for Telegram |

`ClaudeCodeAgent` knows nothing about Telegram or the database — that boundary is intentional and must be preserved.

## Claude Code Integration

Invocation pattern:
```
claude -p "<prompt>" --output-format stream-json --verbose [--resume <session_id>]
```

Key constraints:
- `--verbose` is **required** with `-p` + `--output-format stream-json`; omitting it causes exit code 1.
- `claude_bin` in settings must be an **absolute path** — systemd runs with a minimal PATH.
- Session ID is parsed from the `{"type": "result", "session_id": "..."}` line in stdout.

## Session Decision Logic

Resume current session when **all** conditions are met:
- `active_session_id` exists
- Last activity < 30 minutes ago
- No `/new` flag set
- Message looks like a follow-up (keywords: 继续, 刚才, 再试, continue, fix this too, run again, etc.)

Otherwise: new session.

## Command Protocol

| Command | Behavior |
|---|---|
| `/status` | Current status, task name, cwd, last job time |
| `/stop` | Terminate current subprocess → job=canceled, chat=idle |
| `/new` | Force next message to open a new session |
| `/reset` | Clear active_session_id binding (history kept) |
| `/cwd /path` | Switch working directory, force new session |
| `/task name` | Set active task name |

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
