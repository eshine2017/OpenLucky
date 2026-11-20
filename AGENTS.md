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

## Visual transcript review

After any change that touches `app/command_router.py`, `app/command_help.py`, or any file under `tests/e2e/`, submit the generated transcript HTML as rendered PNG visual artifacts so reviewers can see the conversation flow in the PR's visual review section:

1. Confirm `just test-cov` has run and populated `tests/e2e/artifacts/` with per-test HTML files (test-cov runs the tagged e2e tests as part of the full suite).
2. Run `python3 tools/render_transcripts.py` to generate PNGs alongside each HTML file.
3. For each generated `.png` file, call `submit_visual_artifact` with a descriptive label (e.g. `transcript: test_ping_command`).

Only do this when the change is likely to affect observable bot behavior (new !command, changed reply format, e2e test changes). Skip for pure refactors or unrelated changes.
