# openlucky

A lightweight Telegram bot that uses [Claude Code](https://claude.ai/code) or Gemini as an AI execution engine. Send a message to the bot, it runs the chosen provider on your server, and returns the result back to you in the same chat.

## Requirements

- Python 3.12+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (for the `claude` provider)
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) installed and authenticated (for the `gemini` provider, optional)
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

# AI provider: "claude" (default) or "gemini"
provider: "claude"

# Gemini CLI (only needed when provider is "gemini" or you use !provider gemini)
gemini_bin: "/home/youruser/.local/bin/gemini"   # must be absolute path for systemd
gemini_model: ""   # e.g. "gemini-2.0-flash"; empty = CLI default
```

> **Note:** `claude_bin` and `gemini_bin` must be absolute paths. systemd runs with a minimal PATH and won't find the CLIs by name alone.

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

**Info**

| Command | Description |
|---|---|
| `!status` | Show current session status, task, dir, last summary |
| `!soul` | Show bot identity (SOUL.md) |
| `!whoami` | Show user profile (USER.md) |
| `!memory` | Show long-term memory (MEMORY.md) |
| `!help` | Show all available commands |

**Session**

| Command | Description |
|---|---|
| `!new` | Force next message to start a new session |
| `!reset` | Clear active session binding (history kept) |
| `!stop` | Cancel the currently running task |
| `!cwd <path>` | Switch working directory (forces new session) |
| `!task <name>` | Set active task name |

**Provider**

| Command | Description |
|---|---|
| `!provider [name]` | View or switch AI provider (claude/gemini); starts new session |
| `!model [name]` | View or switch model (opus/sonnet/haiku/fable or full ID); same session |

**Schedule**

| Command | Description |
|---|---|
| `!schedule list` | List all cron jobs with next-run time and last status |
| `!schedule add` | Start interactive flow to create a new cron job |
| `!schedule run <id>` | Trigger a cron job immediately (off-schedule) |
| `!schedule remove <id>` | Delete a cron job |
| `!schedule update <id>` | Start interactive flow to modify an existing cron job |

Any other message is sent to the active AI provider as a prompt. Consecutive messages within 30 minutes resume the same session automatically.

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
