# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**openlucky** is a lightweight Telegram-controlled Claude Code daemon. A long-running daemon receives messages from a Telegram bot, dispatches them to Claude Code as the execution engine, and returns a summary of the result back to the user via the same Telegram chat.

Language: **Python 3.12+**

See README.md for setup and `just` commands.

## Running Tests

Always use `just` — never call pytest or activate the venv directly. Use `just test`, `just test-file <path>`, `just test-cov`, or `just ci`. Run `just --list` for all targets.

## Dev vs Prod

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
Daemon ──► CommandRouter (handles !commands)
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
| `command_router.py` | Parses and executes `!commands`; never touches Claude |
| `session_manager.py` | Decides new vs resume; reads/writes `chats` table |
| `scheduler.py` | Async cron loop; loads spec, computes next runs, calls `run_scheduled_job` |
| `context_builder.py` | Builds system-prompt prefix from SOUL/USER/MEMORY files |
| `agents/claude_code.py` | Spawns subprocess, cancels it, parses stream-json output |
| `agents/gemini_code.py` | Wraps Gemini CLI subprocess; `--resume`/`--session-id` session management |
| `agents/registry.py` | Maps provider name → agent instance; fallback to default |
| `db.py` | SQLite init and CRUD |
| `models.py` | Dataclasses: `Job`, `ChatState`, `RunResult` |
| `config.py` | Loads settings.yaml; respects `CONFIG_FILE` env var |
| `formatter.py` | Formats messages for Telegram |
| `image_store.py` | Saves Telegram photos to `data/images/`; `cleanup_old` prunes files >24 h |

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
- Claude Code's built-in `/model` slash command is **REPL-only** — under `-p` it's just prompt
  text and Claude replies "/model isn't available in this environment." Model selection must
  go through the `--model` flag instead; that's what the `!model` command (`command_router.py`)
  and `ClaudeCodeAgent.default_model` control, not the CLI's own slash command.
- `--model` works with `--resume`: switching a chat's model does not require a new session.

## Session Decision Logic

Resume current session when **all** conditions are met:
- `active_session_id` exists
- Last activity < 30 minutes ago
- No `!new` flag set
- Message looks like a follow-up (keywords: 继续, 刚才, 再试, continue, fix this too, run again, etc.)

Otherwise: new session.

## Command Protocol

| Command | Behavior |
|---|---|
| `!status` | Current status, task name, cwd, last job time |
| `!stop` | Terminate current subprocess → job=canceled, chat=idle |
| `!new` | Force next message to open a new session |
| `!reset` | Clear active_session_id binding (history kept) |
| `!cwd /path` | Switch working directory, force new session |
| `!task name` | Set active task name |
| `!model [name]` | View or switch model (alias: opus/sonnet/haiku/fable, or full ID); same session — `/model` also works |
| `!soul` | Show bot identity (SOUL.md) |
| `!whoami` | Show user profile (USER.md) |
| `!memory` | Show long-term memory (MEMORY.md) |
| `!schedule list` | List all cron jobs with next-run time and last status |
| `!schedule add` | Start a conversational flow to create a new cron job |
| `!schedule run <id>` | Trigger a cron job immediately (off-schedule) |
| `!schedule remove <id>` | Delete a cron job |
| `!schedule update <id>` | Start a conversational flow to modify an existing cron job |
| `!help` | Show all available commands grouped by category |

## Scheduler

The scheduler is a generic cron runner. Each job is just `{id, name, cron_expr, tz, prompt, model}` — no domain coupling.

`model` is optional (empty string = fall back to whatever the chat's `!model` is set to). Pin it
per-job when a scheduled task should always run on a specific model regardless of what the user
is chatting with interactively — e.g. a cheap daily digest pinned to `haiku` while the user chats
on `opus`.

### File split

| File | Owner | Purpose |
|---|---|---|
| `<workspace>/cron.json` | Claude (editable) | Job specs — id, name, enabled, cron_expr, tz, prompt, model |
| `<data>/cron-state.json` | Daemon (runtime) | Next-run timestamps and last-run results |

The spec file is the single source of truth for *what* jobs run and *when*. The state file tracks *runtime* data. Claude can freely edit `cron.json`; the daemon owns `cron-state.json` and never writes `cron.json`.

### Mtime reload

Before each scheduler tick the spec file's mtime is checked. If it changed since last load, the spec is reloaded. This means adding or editing jobs in `cron.json` takes effect within one tick interval (up to 5 minutes — `_MAX_SLEEP_S = 300`) with no daemon restart.

### `!schedule add` conversational flow

1. User sends `!schedule add [optional description]`.
2. `CommandRouter` sets `daemon.pending_actions[chat_id] = "schedule_add"`.
3. The user's next non-command message is intercepted by `Daemon` and routed to `_handle_schedule_add`.
4. `Daemon` calls `ClaudeCodeAgent` with a prompt that includes the SOUL/USER/MEMORY context, the `cron.json` schema, and the user's timezone (read from USER.md).
5. Claude writes the new job directly to `cron.json` and replies with a confirmation.
6. The scheduler reloads on the next tick and begins running the job.

## Second Brain

When `second_brain_dir` is set in settings, Claude is invoked with `--add-dir <path>` and the system prompt routes notes/journal queries there. Empty string disables.

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
