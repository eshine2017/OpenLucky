# Live CLI Smoke Tests

This directory contains opt-in end-to-end smoke tests that exercise the daemon
against the **real** `claude` and `gemini` CLI binaries.

These tests burn real API tokens and are **never run in CI**.

## Prerequisites

- You must already be logged into the CLI (e.g. `claude login`, `gemini auth`).
- The binary must be on your `$PATH` or pointed to by the env vars below.

## Env vars

| Variable | Default | Purpose |
|---|---|---|
| `OPENLUCKY_E2E_LIVE` | *(unset)* | Set to `1` to enable the suite |
| `OPENLUCKY_E2E_LIVE_CLAUDE_BIN` | `claude` on `$PATH` | Absolute path to the `claude` binary |
| `OPENLUCKY_E2E_LIVE_GEMINI_BIN` | `gemini` on `$PATH` | Absolute path to the `gemini` binary |

## Running

```sh
just test-e2e-live
```

This sets `OPENLUCKY_E2E_LIVE=1` and runs `pytest -m e2e_live tests/e2e/live/ -v`.

To run only one provider:

```sh
OPENLUCKY_E2E_LIVE=1 .venv/bin/python -m pytest -m e2e_live tests/e2e/live/test_claude_live.py -v
OPENLUCKY_E2E_LIVE=1 .venv/bin/python -m pytest -m e2e_live tests/e2e/live/test_gemini_live.py -v
```

## What the tests assert

Each test sends one bounded prompt (`"Reply with exactly: OK"`) through the full
daemon stack and verifies:

1. The `send` callback was called at least once with a non-empty message.
2. The job row in SQLite has `status = 'done'`.

Exact LLM reply content is **not** asserted — the tests verify daemon plumbing only.
