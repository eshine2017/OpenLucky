"""Tests for app.scheduler — In-daemon cron scheduler."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from app.scheduler import (
    CronJob,
    CronJobState,
    CronRunRecord,
    CronSchedule,
    Scheduler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_MS = 1_700_000_000_000  # arbitrary fixed epoch ms
_NOW_S = _NOW_MS / 1000


def _fixed_clock() -> float:
    return _NOW_S


def _make_cron_job(
    job_id: str = "test:job",
    *,
    kind: str = "every",
    every_ms: int | None = 60_000,
    expr: str | None = None,
    tz: str | None = None,
    at_ms: int | None = None,
    enabled: bool = True,
    next_run_at_ms: int | None = None,
) -> CronJob:
    schedule = CronSchedule(
        kind=kind,  # type: ignore[arg-type]
        every_ms=every_ms,
        expr=expr,
        tz=tz,
        at_ms=at_ms,
    )
    state = CronJobState(next_run_at_ms=next_run_at_ms)
    return CronJob(
        id=job_id,
        name="Test job",
        schedule=schedule,
        payload={"kind": "test"},
        enabled=enabled,
        state=state,
    )


def _make_scheduler(
    tmp_path,
    *,
    on_job=None,
    clock=None,
) -> Scheduler:
    store_path = str(tmp_path / "scheduler.json")
    if on_job is None:
        on_job = AsyncMock()
    if clock is None:
        clock = _fixed_clock
    return Scheduler(store_path=store_path, on_job=on_job, clock=clock)


# ---------------------------------------------------------------------------
# _compute_next_run
# ---------------------------------------------------------------------------


class TestComputeNextRun:
    def test_at_returns_literal_ms(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        schedule = CronSchedule(kind="at", at_ms=12345)
        assert s._compute_next_run(schedule, _NOW_MS) == 12345

    def test_every_adds_interval(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        schedule = CronSchedule(kind="every", every_ms=5000)
        assert s._compute_next_run(schedule, _NOW_MS) == _NOW_MS + 5000

    def test_cron_returns_next_scheduled_ms(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        # "0 8 * * *" — every day at 08:00
        schedule = CronSchedule(kind="cron", expr="0 8 * * *", tz="UTC")
        result = s._compute_next_run(schedule, _NOW_MS)
        assert result is not None
        assert result > _NOW_MS
        # Should be within 24 hours
        assert result <= _NOW_MS + 24 * 3600 * 1000

    def test_cron_respects_timezone(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        schedule_utc = CronSchedule(kind="cron", expr="0 8 * * *", tz="UTC")
        schedule_pt = CronSchedule(kind="cron", expr="0 8 * * *", tz="America/Los_Angeles")
        result_utc = s._compute_next_run(schedule_utc, _NOW_MS)
        result_pt = s._compute_next_run(schedule_pt, _NOW_MS)
        # PT is UTC-8 (or -7 in DST), so 08:00 PT fires later than 08:00 UTC
        assert result_utc is not None
        assert result_pt is not None
        assert result_pt != result_utc


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_round_trip(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        job = _make_cron_job()
        s.add_job(job)

        # Create a new scheduler from the same store
        s2 = _make_scheduler(tmp_path)
        s2._load()

        jobs = s2.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == job.id
        assert jobs[0].name == job.name
        assert jobs[0].schedule.kind == job.schedule.kind
        assert jobs[0].schedule.every_ms == job.schedule.every_ms
        assert jobs[0].payload == job.payload

    def test_round_trip_with_run_history(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        job = _make_cron_job()
        record = CronRunRecord(run_at_ms=_NOW_MS, status="ok")
        state = CronJobState(
            next_run_at_ms=_NOW_MS + 60_000,
            last_run_at_ms=_NOW_MS,
            last_status="ok",
            run_history=[record],
        )
        stored_job = replace(job, state=state)
        s._jobs[stored_job.id] = stored_job
        s._save()

        s2 = _make_scheduler(tmp_path)
        s2._load()

        reloaded = s2._jobs[job.id]
        assert len(reloaded.state.run_history) == 1
        assert reloaded.state.run_history[0].status == "ok"
        assert reloaded.state.last_status == "ok"


# ---------------------------------------------------------------------------
# ensure_job
# ---------------------------------------------------------------------------


class TestEnsureJob:
    def test_creates_when_missing(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        job = _make_cron_job(job_id="new:job")
        s.ensure_job(job)
        assert "new:job" in {j.id for j in s.list_jobs()}

    def test_no_op_when_exists(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        job = _make_cron_job(job_id="existing:job")
        s.add_job(job)

        # Mutate the stored job's name to detect a clobber
        stored = s._jobs["existing:job"]
        s._jobs["existing:job"] = replace(stored, name="custom name")

        # ensure_job with a different name — must not overwrite
        s.ensure_job(replace(job, name="overwrite attempt"))
        assert s._jobs["existing:job"].name == "custom name"

    def test_persists_new_job(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        s.ensure_job(_make_cron_job(job_id="persist:job"))

        s2 = _make_scheduler(tmp_path)
        s2._load()
        assert any(j.id == "persist:job" for j in s2.list_jobs())


# ---------------------------------------------------------------------------
# _on_timer
# ---------------------------------------------------------------------------


class TestOnTimer:
    async def test_fires_due_jobs(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        # Job with next_run_at_ms in the past
        job = _make_cron_job(next_run_at_ms=_NOW_MS - 1000)
        s._jobs[job.id] = job

        await s._on_timer()

        on_job.assert_awaited_once()

    async def test_skips_disabled_jobs(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        job = _make_cron_job(next_run_at_ms=_NOW_MS - 1000, enabled=False)
        s._jobs[job.id] = job

        await s._on_timer()

        on_job.assert_not_awaited()

    async def test_skips_future_jobs(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        job = _make_cron_job(next_run_at_ms=_NOW_MS + 999_999)
        s._jobs[job.id] = job

        await s._on_timer()

        on_job.assert_not_awaited()

    async def test_updates_state_after_fire(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        job = _make_cron_job(next_run_at_ms=_NOW_MS - 1000, every_ms=5000)
        s._jobs[job.id] = job

        await s._on_timer()

        updated = s._jobs[job.id]
        assert updated.state.last_run_at_ms == _NOW_MS
        assert updated.state.last_status == "ok"
        assert updated.state.next_run_at_ms == _NOW_MS + 5000

    async def test_records_error_status(self, tmp_path) -> None:
        on_job = AsyncMock(side_effect=RuntimeError("boom"))
        s = _make_scheduler(tmp_path, on_job=on_job)

        job = _make_cron_job(next_run_at_ms=_NOW_MS - 1)
        s._jobs[job.id] = job

        await s._on_timer()

        updated = s._jobs[job.id]
        assert updated.state.last_status == "error"
        assert "boom" in (updated.state.last_error or "")

    async def test_caps_run_history_at_20(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        # Pre-populate 20 records
        records = [CronRunRecord(run_at_ms=_NOW_MS - i * 1000, status="ok") for i in range(20)]
        job = _make_cron_job(next_run_at_ms=_NOW_MS - 1)
        job = replace(job, state=replace(job.state, run_history=records))
        s._jobs[job.id] = job

        await s._on_timer()

        assert len(s._jobs[job.id].state.run_history) == 20

    async def test_persists_after_firing(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)
        job = _make_cron_job(next_run_at_ms=_NOW_MS - 1)
        s._jobs[job.id] = job

        await s._on_timer()

        assert os.path.exists(s._store_path)
        with open(s._store_path) as fh:
            data = json.load(fh)
        assert len(data["jobs"]) == 1


# ---------------------------------------------------------------------------
# run_now
# ---------------------------------------------------------------------------


class TestRunNow:
    async def test_fires_regardless_of_next_run(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        # Job scheduled far in the future
        job = _make_cron_job(next_run_at_ms=_NOW_MS + 999_999_999)
        s._jobs[job.id] = job

        result = await s.run_now(job.id)

        assert result is True
        on_job.assert_awaited_once()

    async def test_returns_false_for_unknown_id(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        result = await s.run_now("nonexistent:job")

        assert result is False
        on_job.assert_not_awaited()

    async def test_updates_state_after_run_now(self, tmp_path) -> None:
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)
        job = _make_cron_job(job_id="rn:job", next_run_at_ms=_NOW_MS + 999_999)
        s._jobs[job.id] = job

        await s.run_now(job.id)

        updated = s._jobs[job.id]
        assert updated.state.last_run_at_ms == _NOW_MS
        assert updated.state.last_status == "ok"


# ---------------------------------------------------------------------------
# start() recomputes stale next_run_at_ms
# ---------------------------------------------------------------------------


class TestStartRecompute:
    async def test_stale_next_run_reset_on_start(self, tmp_path) -> None:
        """A stale (past) next_run_at_ms must be recomputed, not fired in burst."""
        on_job = AsyncMock()
        s = _make_scheduler(tmp_path, on_job=on_job)

        # Plant a job with a very old next_run_at_ms
        stale_ms = _NOW_MS - 7 * 24 * 3600 * 1000  # 7 days ago
        job = _make_cron_job(next_run_at_ms=stale_ms)
        s._jobs[job.id] = job
        s._save()

        # Create a fresh scheduler and call start (with _running=True to arm timer)
        s2 = _make_scheduler(tmp_path, on_job=on_job)
        # Patch _arm_timer to prevent actually creating an asyncio task in test
        s2._arm_timer = MagicMock()
        await s2.start()

        reloaded = s2._jobs[job.id]
        # next_run_at_ms must have been recomputed to the future
        assert reloaded.state.next_run_at_ms is not None
        assert reloaded.state.next_run_at_ms > _NOW_MS
        # on_job must NOT have been called (no burst fire)
        on_job.assert_not_awaited()


# ---------------------------------------------------------------------------
# set_enabled / remove_job / list_jobs
# ---------------------------------------------------------------------------


class TestMutations:
    def test_set_enabled_disables(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        s.add_job(_make_cron_job(job_id="j1"))
        assert s.set_enabled("j1", False) is True
        assert s._jobs["j1"].enabled is False

    def test_set_enabled_unknown(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        assert s.set_enabled("nope", True) is False

    def test_remove_job(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        s.add_job(_make_cron_job(job_id="del:me"))
        assert s.remove_job("del:me") is True
        assert "del:me" not in {j.id for j in s.list_jobs()}

    def test_remove_unknown(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        assert s.remove_job("ghost") is False

    def test_list_jobs_empty(self, tmp_path) -> None:
        s = _make_scheduler(tmp_path)
        assert s.list_jobs() == []
