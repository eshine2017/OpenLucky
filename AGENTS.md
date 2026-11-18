# AGENTS.md

Agent guide for openlucky. Architecture detail lives in `CLAUDE.md` — read that first.

## Commands

Always use `just`. Never call `pytest` directly or activate the venv manually.

| Task | Command |
|---|---|
| Run all checks | `just ci` |
| Run tests | `just test` |
| Run a single file | `just test-file <path>` |
| Tests with coverage | `just test-cov` |
| List all targets | `just --list` |

**Run `just ci` before finishing any task.**

## Code standards

- Python 3.12+
- Ruff line length: 100
- New behavior requires tests in the same commit — no exceptions.

## Isolation boundary

`ClaudeCodeAgent` (`agents/claude_code.py`) must stay ignorant of Telegram and the database. Do not pass PTB objects, chat IDs, or DB handles into it. This boundary is load-bearing for testability.

## Secrets

`config/settings.yaml` and `config/settings.dev.yaml` are gitignored. Never commit them. Use the `.example` files as templates.

## MVP scope (v1 limits)

Do not add:
- Multi-user permission system
- Token-by-token streaming
- Parallel sessions
- Complex memory or summarization
- Auto repo discovery
