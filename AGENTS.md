# AGENTS.md

Agent guide for the **openlucky** repository. Architecture detail lives in [CLAUDE.md](CLAUDE.md) — read it first.

## Commands

Always use `just`. Never call pytest directly or activate the venv yourself.

| Command | Purpose |
|---|---|
| `just test` | Run the test suite |
| `just test-file <path>` | Run a single test file |
| `just test-cov` | Run tests with coverage (80% floor) |
| `just lint` | Ruff linting |
| `just typecheck` | mypy strict type checking |
| `just ci` | Lint + typecheck + test-cov (run this before finishing) |

Run `just --list` to see all available targets.

## Before finishing

Run `just ci`. It runs ruff, mypy strict, and pytest with an 80% coverage floor. All three must pass.

## Agent boundary

`ClaudeCodeAgent` and all modules under `app/agents/` must know nothing about Telegram or the database. That boundary is intentional — do not cross it. See CLAUDE.md for the full module responsibility table.

## MVP scope limits

Do not add in v1 (see CLAUDE.md for the full list):
- Multi-user permission system
- Token-by-token streaming
- Parallel sessions
- Complex memory/summarization
- Auto repo discovery

## Config and secrets

`config/settings.yaml` and `config/settings.dev.yaml` are gitignored. Use the `.example` files as templates. Never commit secrets, tokens, or real config files.

## Style

- Python 3.12+
- Ruff line length: 100
- New or changed behavior must have tests under `tests/`
