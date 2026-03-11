# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**openlucky** is a lightweight Telegram-controlled Claude Code daemon. A long-running daemon receives messages from a Telegram bot, dispatches them to Claude Code as the execution engine, and returns a summary of the result back to the user via the same Telegram chat.

Language: **Python**

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
data/
  app.db
  logs/
  jobs/               # raw output files per job
config/
  settings.yaml       # TELEGRAM_BOT_TOKEN, ALLOWED_USERS, WORK_DIR, CLAUDE_BIN
```

## Database Schema (SQLite)

```sql
CREATE TABLE chats (
  telegram_chat_id TEXT PRIMARY KEY,
  active_session_id TEXT,
  active_task_name TEXT,
  cwd TEXT,
  status TEXT,          -- idle / running / error
  last_active_at TEXT,
  last_summary TEXT
);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY,
  telegram_chat_id TEXT,
  session_id TEXT,
  user_message TEXT,
  status TEXT,          -- queued / running / done / failed / canceled
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  result_summary TEXT,
  raw_output_path TEXT
);

CREATE TABLE session_history (
  session_id TEXT PRIMARY KEY,
  telegram_chat_id TEXT,
  task_name TEXT,
  cwd TEXT,
  created_at TEXT,
  last_active_at TEXT,
  is_archived INTEGER
);
```

## In-Memory State

```python
running_locks: dict[str, str]   # chat_id → job_id
live_processes: dict[str, Process]  # job_id → subprocess
```

One chat = one running job at a time. If a new message arrives while a job is running, reject it with a prompt to `/stop`.

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
- `cwd` unchanged
- No `/new` flag set
- Message looks like a follow-up (keywords: 继续, 刚才, 再试, continue, fix this too, run again, etc.)

Otherwise: new session.

## Claude Code Integration

`claude_runner.py` has exactly one responsibility: build the command, spawn the subprocess, collect output, parse `session_id`. It knows nothing about Telegram or the database.

```python
@dataclass
class RunResult:
    session_id: str
    stdout: str
    stderr: str
    exit_code: int
    summary: str

class ClaudeRunner:
    def run_new(self, prompt: str, cwd: str) -> RunResult: ...
    def run_resume(self, session_id: str, prompt: str, cwd: str) -> RunResult: ...
```

Invoke Claude Code with:
```
claude -p "<prompt>" --output-format stream-json
claude -p "<prompt>" --output-format stream-json --resume <session_id>
```

## Result Format Sent to Telegram

Do **not** send full stdout. Three-phase response:

1. **Start**: `开始处理: <task_name>\n模式: resume/new\n目录: <cwd>`
2. **Running**: `正在执行中...`
3. **Done**: Short summary (3–5 bullet points) + exit code. Full log path if needed.

## MVP Constraints (intentional scope limits)

Do NOT add in v1:
- Multi-user permission system
- True streaming token-by-token forwarding
- Parallel sessions
- Complex memory/summarization
- Auto repo discovery
- Async worker pool (a background thread/task per job is sufficient for single-user)
