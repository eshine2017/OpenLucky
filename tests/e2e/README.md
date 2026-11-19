# E2E Tests

End-to-end tests that exercise the full daemon stack with fake CLI stubs.
No real `claude` or `gemini` binaries are required.

Run with:

```
just test-e2e
```

## Fixtures

The main fixture is `e2e_daemon`, which builds an isolated real `Daemon` wired to a
real SQLite database, `SessionManager`, `AgentRegistry`, and a fake CLI binary.
It returns `(daemon, send_mock)`.

For tests that need a custom CLI binary (e.g. `recording_claude_bin`), use
`make_e2e_daemon` — a factory fixture that accepts `claude_bin` and `seed_workspace`
arguments and returns `(daemon, send_mock, settings)`.

## Viewing transcripts

Some tests are tagged `@pytest.mark.record_transcript`.  After running those tests,
a Telegram-style HTML page is written to `tests/e2e/artifacts/` for each tagged test,
plus an `index.html` listing all generated transcripts with their pass/fail status.

### Opting in

Add both markers to a test:

```python
@pytest.mark.e2e
@pytest.mark.record_transcript
def test_my_scenario(transcript_recorder) -> None:
    daemon, send_mock, recorder = transcript_recorder
    recorder.send_user("42", "hello")
    ...
```

For tests that build their daemon via `make_e2e_daemon`, use `make_transcript_recorder`
instead:

```python
@pytest.mark.e2e
@pytest.mark.record_transcript
def test_my_scenario(make_e2e_daemon, make_transcript_recorder) -> None:
    daemon, send_mock, settings = make_e2e_daemon(claude_bin=..., seed_workspace=True)
    recorder = make_transcript_recorder(daemon, send_mock)
    recorder.send_user("42", "hello")
    ...
```

### Design choice

`e2e_daemon`'s `(daemon, send_mock)` return shape is preserved unchanged.
`transcript_recorder` is a separate fixture that wraps `e2e_daemon` and yields
`(daemon, send_mock, recorder)`.  `make_transcript_recorder` is a factory fixture
for the `make_e2e_daemon` case.  This lets tests migrate incrementally — existing
tests keep working without any change.

### How recording works

- **Inbound** (user → bot): call `recorder.send_user(chat_id, text)` instead of
  `daemon.on_message(chat_id, text)`.  Both routes work, but only `send_user` entries
  appear in the HTML.
- **Outbound** (bot → user): the fixture wraps `send_mock`'s `side_effect` to capture
  every `send(chat_id, text)` call automatically.  Existing
  `send_mock.call_args_list` assertions are unaffected.

### Artifact path

```
tests/e2e/artifacts/
  index.html                         ← lists all transcripts + pass/fail
  tests_e2e_test_happy_path_py__...html
  tests_e2e_test_session_continuity_py__...html
```

The `artifacts/` directory is gitignored.

### Rendering via Syrus

When running inside a Syrus workflow, the implementation agent calls
`submit_artifact` to attach the generated HTML files to the PR's test plan,
so reviewers can inspect the transcripts directly from the PR page.
