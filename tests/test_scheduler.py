"""Tests for app.scheduler — In-daemon cron scheduler (generic spec+state split)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from app.scheduler import (
    CronJobState,
    CronRunRecord,
    Scheduler,
    _compute_next_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_MS = 1_700_000_000_000  # arbitrary fixed epoch ms
_NOW_S = _NOW_MS / 1000


def _fixed_clock() -> float:
    return _NOW_S


def _write_spec(path: str, jobs: list[dict]) -> None:
    with open(path, "w") as f:
        json.dump({"jobs": jobs}, f)


def _make_job_spec(
    job_id: str = "test:job",
    *,
    cron_expr: str = "0 8 * * *",
    tz: str = "UTC",
    prompt: str = "hello",
    enabled: bool = True,
    name: str = "Test",
) -> dict:
    return {
        "id": job_id,
        "name": name,
        "enabled": enabled,
        "cron_expr": cron_expr,
        "tz": tz,
        "prompt": prompt,
    }


def _make_scheduler(
    tmp_path,
    *,
    on_job=None,
    clock=None,
) -> Scheduler:
    spec_path = str(tmp_path / "cron.json")
    state_path = str(tmp_path / "cron-state.json")
    if on_job is None:
        on_job = AsyncMock()
    if clock is None:
        clock = _fixed_clock
    return Scheduler(spec_path=spec_path, state_path=state_path, on_job=on_job, clock=clock)


# ---------------------------------------------------------------------------
# _compute_next_run (module-level function)
# ---------------------------------------------------------------------------


class TestComputeNextRun:
    def test_valid_cron_returns_future_ms(self) -> None:
        # "0 8 * * *" — every day at 08:00 UTC
        result = _compute_next_run("0 8 * * *", "UTC", _NOW_MS)
        assert result is not None
        assert result > _NOW_MS
        # Should be within 24 hours
        assert result <= _NOW_MS + 24 * 3600 * 1000

    def test_respects_timezone(self) -> None:
        result_utc = _compute_next_run("0 8 * * *", "UTC", _NOW_MS)
        result_pt = _compute_next_run("0 8 * * *", "America/Los_Angeles", _NOW_MS)
        assert result_utc is not None
        assert result_pt is not None
        # PT fires later than UTC for the same wall-clock time
        assert result_pt != result_utc

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        # Should not raise; falls back to UTC
        result = _compute_next_run("0 8 * * *", "Not/A/Real/Zone", _NOW_MS)
        utc_result = _compute_next_run("0 8 * * *", "UTC", _NOW_MS)
        assert result is not None
        assert result == utc_result

    def test_invalid_cron_expression_returns_none(self) -> None:
        result = _compute_next_run("not a cron expr !! @ #", "UTC", _NOW_MS)
        assert result is None

    def test_five_field_cron(self) -> None:
        # Standard 5-field cron
        result = _compute_next_run("*/5 * * * *", "UTC", _NOW_MS)
        assert result is not None
        # Should fire within 5 minutes
        assert result <= _NOW_MS + 5 * 60 * 1000


# ---------------------------------------------------------------------------
# Scheduler constructor and spec/state paths
# ---------------------------------------------------------------------------


class TestSchedulerConstructor:
    def test_has_spec_path_and_state_path(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        assert s._spec_path == str(tmp_path / "cron.json")
        assert s._state_path == str(tmp_path / "cron-state.json")

    def test_has_loop_attribute(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        assert hasattr(s, "_loop")
        assert s._loop is None


# ---------------------------------------------------------------------------
# start() — loads spec and state, recomputes next_run
# ---------------------------------------------------------------------------


class TestStart:
    async def test_loads_spec_from_spec_path(self, tmp_path) -> None:
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])
        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()
        jobs = s.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "j1"

    async def test_loads_state_from_state_path(self, tmp_path) -> None:
        spec_path = str(tmp_path / "cron.json")
        state_path = str(tmp_path / "cron-state.json")
        _write_spec(spec_path, [_make_job_spec("j1")])
        # Pre-seed state
        state_data = {
            "j1": {
                "next_run_at_ms": _NOW_MS + 10_000,
                "last_run_at_ms": _NOW_MS - 100,
                "last_status": "ok",
                "last_error": None,
                "run_history": [],
            }
        }
        with open(state_path, "w") as f:
            json.dump(state_data, f)

        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()
        jobs = s.list_jobs()
        assert len(jobs) == 1
        # start() recomputes next_run, so last_run_at_ms should still be from state
        assert jobs[0].state.last_run_at_ms == _NOW_MS - 100
        assert jobs[0].state.last_status == "ok"

    async def test_stale_next_run_reset_on_start(self, tmp_path) -> None:
        """A stale (past) next_run_at_ms must be recomputed, not fired in burst."""
        spec_path = str(tmp_path / "cron.json")
        state_path = str(tmp_path / "cron-state.json")
        _write_spec(spec_path, [_make_job_spec("j1")])
        # Plant a stale next_run_at_ms
        stale_ms = _NOW_MS - 7 * 24 * 3600 * 1000  # 7 days ago
        state_data = {
            "j1": {
                "next_run_at_ms": stale_ms,
                "last_run_at_ms": None,
                "last_status": None,
                "last_error": None,
                "run_history": [],
            }
        }
        with open(state_path, "w") as f:
            json.dump(state_data, f)

        on_job = AsyncMock()
        s = Scheduler(
            spec_path=str(tmp_path / "cron.json"),
            state_path=str(tmp_path / "cron-state.json"),
            on_job=on_job,
            clock=_fixed_clock,
        )
        s._arm_timer = MagicMock()
        await s.start()

        jobs = s.list_jobs()
        assert jobs[0].state.next_run_at_ms is not None
        assert jobs[0].state.next_run_at_ms > _NOW_MS
        on_job.assert_not_awaited()

    async def test_start_with_no_spec_file(self, tmp_path) -> None:
        """Scheduler starts cleanly when spec file doesn't exist."""
        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()
        assert s.list_jobs() == []


# ---------------------------------------------------------------------------
# list_jobs() — merged spec + state view
# ---------------------------------------------------------------------------


class TestListJobs:
    async def test_returns_merged_spec_and_state(self, tmp_path) -> None:
        spec_path = str(tmp_path / "cron.json")
        state_path = str(tmp_path / "cron-state.json")
        _write_spec(spec_path, [_make_job_spec("j1", prompt="do something")])
        state_data = {
            "j1": {
                "next_run_at_ms": _NOW_MS + 1000,
                "last_run_at_ms": _NOW_MS - 500,
                "last_status": "ok",
                "last_error": None,
                "run_history": [],
            }
        }
        with open(state_path, "w") as f:
            json.dump(state_data, f)

        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()

        jobs = s.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "j1"
        assert jobs[0].prompt == "do something"
        assert jobs[0].state.last_status == "ok"
        assert jobs[0].state.last_run_at_ms == _NOW_MS - 500

    def test_returns_empty_without_loading(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        assert s.list_jobs() == []


# ---------------------------------------------------------------------------
# _on_timer() — fires due jobs, reloads spec if changed
# ---------------------------------------------------------------------------


class TestOnTimer:
    async def test_fires_due_jobs(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        # Force next_run to the past
        job_state = s._state.get("j1", CronJobState())
        s._state["j1"] = replace(job_state, next_run_at_ms=_NOW_MS - 1000)

        await s._on_timer()

        on_job.assert_awaited_once()

    async def test_skips_disabled_jobs(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1", enabled=False)])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS - 1000)

        await s._on_timer()

        on_job.assert_not_awaited()

    async def test_skips_future_jobs(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS + 999_999)

        await s._on_timer()

        on_job.assert_not_awaited()

    async def test_updates_state_after_fire(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS - 1000)

        await s._on_timer()

        state = s._state["j1"]
        assert state.last_run_at_ms == _NOW_MS
        assert state.last_status == "ok"
        assert state.next_run_at_ms is not None
        assert state.next_run_at_ms > _NOW_MS

    async def test_records_error_status(self, tmp_path) -> None:
        on_job = AsyncMock(side_effect=RuntimeError("boom"))
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS - 1)

        await s._on_timer()

        state = s._state["j1"]
        assert state.last_status == "error"
        assert "boom" in (state.last_error or "")

    async def test_caps_run_history_at_20(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        records = [CronRunRecord(run_at_ms=_NOW_MS - i * 1000, status="ok") for i in range(20)]
        s._state["j1"] = CronJobState(
            next_run_at_ms=_NOW_MS - 1, run_history=records
        )

        await s._on_timer()

        assert len(s._state["j1"].run_history) == 20

    async def test_persists_state_after_firing(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        state_path = str(tmp_path / "cron-state.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS - 1)

        await s._on_timer()

        assert os.path.exists(state_path)
        with open(state_path) as fh:
            data = json.load(fh)
        assert "j1" in data
        assert data["j1"]["last_status"] == "ok"

    async def test_reloads_spec_on_mtime_change(self, tmp_path) -> None:
        """Scheduler picks up a new job added to spec file (mtime changed)."""
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        assert len(s._spec) == 1

        # Write a new spec with an additional job
        import time
        time.sleep(0.01)  # ensure mtime differs
        _write_spec(spec_path, [_make_job_spec("j1"), _make_job_spec("j2")])
        # Manually bump the mtime to guarantee the check fires
        new_mtime = os.path.getmtime(spec_path) + 1
        os.utime(spec_path, (new_mtime, new_mtime))

        await s._on_timer()

        assert len(s._spec) == 2
        assert any(j.id == "j2" for j in s._spec)


# ---------------------------------------------------------------------------
# run_now()
# ---------------------------------------------------------------------------


class TestRunNow:
    async def test_fires_regardless_of_next_run(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        # Set next_run far in the future
        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS + 999_999_999)

        result = await s.run_now("j1")

        assert result is True
        on_job.assert_awaited_once()

    async def test_returns_false_for_unknown_id(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        result = await s.run_now("nonexistent:job")

        assert result is False
        on_job.assert_not_awaited()

    async def test_updates_state_after_run_now(self, tmp_path) -> None:
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS + 999_999)

        await s.run_now("j1")

        state = s._state["j1"]
        assert state.last_run_at_ms == _NOW_MS
        assert state.last_status == "ok"

    async def test_reloads_spec_before_run_now(self, tmp_path) -> None:
        """run_now reloads spec if mtime changed before firing."""
        on_job = AsyncMock()
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        # Add j2 to spec and bump mtime
        _write_spec(spec_path, [_make_job_spec("j1"), _make_job_spec("j2")])
        new_mtime = os.path.getmtime(spec_path) + 1
        os.utime(spec_path, (new_mtime, new_mtime))

        result = await s.run_now("j2")

        assert result is True
        on_job.assert_awaited_once()


# ---------------------------------------------------------------------------
# set_enabled()
# ---------------------------------------------------------------------------


class TestSetEnabled:
    async def test_set_enabled_false_updates_spec_file(self, tmp_path) -> None:
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()

        result = s.set_enabled("j1", False)
        assert result is True

        # Verify spec file was updated
        with open(spec_path) as f:
            data = json.load(f)
        assert data["jobs"][0]["enabled"] is False

        # In-memory should also reflect
        assert s._spec[0].enabled is False

    async def test_set_enabled_unknown_returns_false(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()

        result = s.set_enabled("ghost", True)
        assert result is False


# ---------------------------------------------------------------------------
# remove_job()
# ---------------------------------------------------------------------------


class TestRemoveJob:
    async def test_removes_from_spec_file_and_state(self, tmp_path) -> None:
        spec_path = str(tmp_path / "cron.json")
        state_path = str(tmp_path / "cron-state.json")
        _write_spec(spec_path, [_make_job_spec("j1"), _make_job_spec("j2")])

        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()

        # Seed some state for j1
        s._state["j1"] = CronJobState(last_status="ok")
        s._save_state()

        result = s.remove_job("j1")
        assert result is True

        # Spec file should only have j2
        with open(spec_path) as f:
            data = json.load(f)
        ids = [j["id"] for j in data["jobs"]]
        assert "j1" not in ids
        assert "j2" in ids

        # State file should not have j1
        with open(state_path) as f:
            state_data = json.load(f)
        assert "j1" not in state_data

        # In-memory spec should not have j1
        assert not any(j.id == "j1" for j in s._spec)

    async def test_remove_unknown_returns_false(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        s._arm_timer = MagicMock()
        await s.start()

        result = s.remove_job("ghost")
        assert result is False


# ---------------------------------------------------------------------------
# State persistence round-trip
# ---------------------------------------------------------------------------


class TestStatePersistence:
    async def test_state_round_trip(self, tmp_path) -> None:
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        # Fire the job
        s._state["j1"] = CronJobState(next_run_at_ms=_NOW_MS - 1)
        await s._on_timer()

        # Load a fresh scheduler and verify state is persisted
        s2 = _make_scheduler(tmp_path, on_job=AsyncMock())
        s2._load_state()

        state = s2._state.get("j1")
        assert state is not None
        assert state.last_status == "ok"
        assert state.last_run_at_ms == _NOW_MS

    async def test_state_survives_spec_reload(self, tmp_path) -> None:
        """State persists independently of spec reloads."""
        spec_path = str(tmp_path / "cron.json")
        _write_spec(spec_path, [_make_job_spec("j1")])

        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)
        s._arm_timer = MagicMock()
        await s.start()

        # Set some state
        s._state["j1"] = CronJobState(last_status="ok", last_run_at_ms=_NOW_MS - 100)
        s._save_state()

        # Reload spec (as if mtime changed)
        s._load_spec()

        # State should be unchanged
        assert s._state["j1"].last_status == "ok"
        assert s._state["j1"].last_run_at_ms == _NOW_MS - 100
