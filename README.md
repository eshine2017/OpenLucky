# openlucky

A lightweight Telegram bot that controls [Claude Code](https://claude.ai/code) as an execution engine. Send a message to the bot, it runs Claude Code on your server, and returns the result back to you in the same chat.

## Requirements

- Python 3.12+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- [`just`](https://github.com/casey/just) command runner (recommended)

## Setup

```bash
git clone https://github.com/eshine2017/OpenLucky.git
cd OpenLucky
just venv
cp config/settings.yaml.example config/settings.yaml
```

Edit `config/settings.yaml`:

```yaml
telegram_bot_token: "YOUR_BOT_TOKEN"
allowed_users: [123456789]   # your Telegram user ID — get it from @userinfobot
work_dir: "/home/youruser/projects"
claude_bin: "/home/youruser/.local/bin/claude"   # must be absolute path
session_timeout_minutes: 30
log_level: "INFO"
```

> **Note:** `claude_bin` must be an absolute path. systemd runs with a minimal PATH and won't find `claude` by name alone.

Optionally, set `second_brain_dir` to give the bot read/write access to a notes directory (e.g. an Obsidian vault). Claude will be told to read and modify files there when you ask about notes, journal entries, or knowledge. Leave it empty (the default) to disable.

```yaml
second_brain_dir: "/home/youruser/vault"  # optional; leave empty to disable
```

## Running

**Dev (foreground):**
```bash
just dev
```

**Prod (systemd):**
```bash
just service-install
just service-logs     # follow logs
```

For a separate dev bot, copy `config/settings.dev.yaml.example` to `config/settings.dev.yaml` — `just dev` picks it up automatically.

## Commands

| Command | Description |
|---|---|
| `!status` | Show current session, task, and working directory |
| `!stop` | Cancel the running job |
| `!new` | Force the next message to start a new session |
| `!reset` | Clear the current session binding (history preserved) |
| `!cwd /path/to/dir` | Change working directory (forces new session) |
| `!task <name>` | Set a label for the current task |
| `!soul` | Show bot identity |
| `!whoami` | Show user profile |
| `!memory` | Show long-term memory |

Any other message is sent to Claude Code as a prompt. Consecutive messages within 30 minutes resume the same session automatically.

## Development

All workflows go through `just` — do not invoke `python` or `pytest` directly, and do not source `.venv` manually.

```bash
just test                    # run tests
just test-file <path>        # run a specific test file
just test-cov                # run with coverage (fails below 80%)
just ci                      # lint + typecheck + tests with coverage
just dev-reset               # clear dev DB and job logs
just db-shell                # open SQLite shell on dev DB
```

Run `just --list` to see all available commands.
