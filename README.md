# openlucky

A lightweight Telegram bot that controls [Claude Code](https://claude.ai/code) as an execution engine. Send a message to the bot, it runs Claude Code on your server, and returns the result back to you in the same chat.

## Requirements

- Python 3.12+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (`claude` on PATH)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
git clone https://github.com/eshine2017/OpenLucky.git
cd OpenLucky
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/settings.yaml.example config/settings.yaml
```

## Configuration

Edit `config/settings.yaml`:

```yaml
telegram_bot_token: "YOUR_BOT_TOKEN"
allowed_users: [123456789]   # your Telegram user ID — get it from @userinfobot
work_dir: "/home/youruser/projects"
claude_bin: "claude"
session_timeout_minutes: 30
log_level: "INFO"
```

## Running

**Dev (foreground):**
```bash
source .venv/bin/activate
python3 -m app.main
```

**Prod (systemd):**
```bash
sudo cp openlucky.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable openlucky
sudo systemctl start openlucky
sudo journalctl -u openlucky -f   # follow logs
```

For a separate dev bot, copy `config/settings.dev.yaml.example` to `config/settings.dev.yaml` and run:
```bash
CONFIG_FILE=config/settings.dev.yaml python3 -m app.main
```

## Commands

| Command | Description |
|---|---|
| `/status` | Show current session, task, and working directory |
| `/stop` | Cancel the running job |
| `/new` | Force the next message to start a new session |
| `/reset` | Clear the current session binding (history preserved) |
| `/cwd /path/to/dir` | Change working directory (forces new session) |
| `/task <name>` | Set a label for the current task |

Any other message is sent to Claude Code as a prompt. Consecutive messages within 30 minutes resume the same session automatically.
