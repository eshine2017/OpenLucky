"""
scheduler.py — In-daemon cron scheduler.

Runs on PTB's asyncio event loop. All state lives on that loop; command
handlers that mutate scheduler state (future !schedule add/remove) must hop
via asyncio.run_coroutine_threadsafe(coro, loop) — do NOT call mutating
methods from a thread pool directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

_MAX_SLEEP_S = 300  # 5 minutes — caps sleep to bound clock drift
_MAX_HISTORY = 20


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CronRunRecord:
    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    error: str | None = None


@dataclass
class CronJobState:
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)


@dataclass
class CronSchedule:
    kind: Literal["at", "every", "cron"]
    at_ms: int | None = None
    every_ms: int | None = None
    expr: str | None = None
    tz: str | None = None


@dataclass
class CronJob:
    id: str
    name: str
    schedule: CronSchedule
    payload: dict[str, Any]
    enabled: bool = True
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _record_to_dict(r: CronRunRecord) -> dict[str, Any]:
    return {"run_at_ms": r.run_at_ms, "status": r.status, "error": r.error}


def _record_from_dict(d: dict[str, Any]) -> CronRunRecord:
    return CronRunRecord(
        run_at_ms=d["run_at_ms"],
        status=d["status"],
        error=d.get("error"),
    )


def _state_to_dict(s: CronJobState) -> dict[str, Any]:
    return {
        "next_run_at_ms": s.next_run_at_ms,
        "last_run_at_ms": s.last_run_at_ms,
        "last_status": s.last_status,
        "last_error": s.last_error,
        "run_history": [_record_to_dict(r) for r in s.run_history],
    }


def _state_from_dict(d: dict[str, Any]) -> CronJobState:
    return CronJobState(
        next_run_at_ms=d.get("next_run_at_ms"),
        last_run_at_ms=d.get("last_run_at_ms"),
        last_status=d.get("last_status"),
        last_error=d.get("last_error"),
        run_history=[_record_from_dict(r) for r in d.get("run_history", [])],
    )


def _schedule_to_dict(s: CronSchedule) -> dict[str, Any]:
    return {
        "kind": s.kind,
        "at_ms": s.at_ms,
        "every_ms": s.every_ms,
        "expr": s.expr,
        "tz": s.tz,
    }


def _schedule_from_dict(d: dict[str, Any]) -> CronSchedule:
    return CronSchedule(
        kind=d["kind"],
        at_ms=d.get("at_ms"),
        every_ms=d.get("every_ms"),
        expr=d.get("expr"),
        tz=d.get("tz"),
    )


def _job_to_dict(job: CronJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": _schedule_to_dict(job.schedule),
        "payload": job.payload,
        "state": _state_to_dict(job.state),
        "created_at_ms": job.created_at_ms,
        "updated_at_ms": job.updated_at_ms,
    }


def _job_from_dict(d: dict[str, Any]) -> CronJob:
    return CronJob(
        id=d["id"],
        name=d["name"],
        enabled=d.get("enabled", True),
        schedule=_schedule_from_dict(d["schedule"]),
        payload=d.get("payload", {}),
        state=_state_from_dict(d.get("state", {})),
        created_at_ms=d.get("created_at_ms", 0),
        updated_at_ms=d.get("updated_at_ms", 0),
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    def __init__(
        self,
        store_path: str,
        on_job: Callable[[CronJob], Awaitable[None]],
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store_path = store_path
        self._on_job = on_job
        self._clock = clock or time.time
        self._jobs: dict[str, CronJob] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None  # set by main.py post_init

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load store, recompute next_run_at_ms for all jobs, then arm the timer."""
        self._load()

        # Recompute from now so a daemon restart never fires a burst of missed runs.
        now_ms = int(self._clock() * 1000)
        for job_id, job in list(self._jobs.items()):
            next_ms = self._compute_next_run(job.schedule, now_ms)
            self._jobs[job_id] = replace(job, state=replace(job.state, next_run_at_ms=next_ms))
        self._save()

        self._running = True
        self._arm_timer()
        logger.info("Scheduler started with %d jobs", len(self._jobs))

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            async with contextlib.suppress(asyncio.CancelledError):  # type: ignore[attr-defined]
                await self._task
        self._task = None
        logger.info("Scheduler stopped")

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _arm_timer(self) -> None:
        if not self._running:
            return
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.get_running_loop().create_task(self._tick())

    async def _tick(self) -> None:
        now_ms = int(self._clock() * 1000)
        due_ms: int | None = None

        for job in self._jobs.values():
            if (
                job.enabled
                and job.state.next_run_at_ms is not None
                and (due_ms is None or job.state.next_run_at_ms < due_ms)
            ):
                due_ms = job.state.next_run_at_ms

        sleep_s: float
        if due_ms is None:
            sleep_s = float(_MAX_SLEEP_S)
        else:
            sleep_s = min(max((due_ms - now_ms) / 1000, 0.0), float(_MAX_SLEEP_S))

        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            return

        if self._running:
            await self._on_timer()

    async def _on_timer(self) -> None:
        now_ms = int(self._clock() * 1000)

        due = [
            job
            for job in self._jobs.values()
            if job.enabled
            and job.state.next_run_at_ms is not None
            and job.state.next_run_at_ms <= now_ms
        ]

        for job in due:
            try:
                await self._on_job(job)
                status: Literal["ok", "error", "skipped"] = "ok"
                error: str | None = None
            except Exception as exc:  # noqa: BLE001
                status = "error"
                error = str(exc)
                logger.error("Scheduled job %s failed: %s", job.id, exc)

            record = CronRunRecord(run_at_ms=now_ms, status=status, error=error)
            history = (job.state.run_history + [record])[-_MAX_HISTORY:]
            next_ms = self._compute_next_run(job.schedule, now_ms)
            new_state = CronJobState(
                next_run_at_ms=next_ms,
                last_run_at_ms=now_ms,
                last_status=status,
                last_error=error,
                run_history=history,
            )
            self._jobs[job.id] = replace(job, state=new_state)

        self._save()

        if self._running:
            self._arm_timer()

    # ------------------------------------------------------------------
    # Schedule computation
    # ------------------------------------------------------------------

    def _compute_next_run(self, schedule: CronSchedule, now_ms: int) -> int | None:
        if schedule.kind == "at":
            return schedule.at_ms
        if schedule.kind == "every":
            return now_ms + (schedule.every_ms or 0)
        if schedule.kind == "cron":
            return self._next_cron_ms(schedule.expr or "0 8 * * *", schedule.tz, now_ms)
        return None

    @staticmethod
    def _next_cron_ms(expr: str, tz_name: str | None, now_ms: int) -> int:
        from zoneinfo import ZoneInfo

        from croniter import croniter  # type: ignore[import-untyped]

        if tz_name:
            tz = ZoneInfo(tz_name)
            now_dt = datetime.fromtimestamp(now_ms / 1000, tz=tz)
        else:
            now_dt = datetime.fromtimestamp(now_ms / 1000, tz=UTC)

        cron = croniter(expr, now_dt)
        next_dt: datetime = cron.get_next(datetime)
        return int(next_dt.timestamp() * 1000)

    # ------------------------------------------------------------------
    # Public job management
    # ------------------------------------------------------------------

    def ensure_job(self, job: CronJob) -> None:
        """Insert job only if its id is not already stored. Never overwrites."""
        if job.id not in self._jobs:
            self.add_job(job)

    def add_job(self, job: CronJob) -> None:
        now_ms = int(self._clock() * 1000)
        next_ms = self._compute_next_run(job.schedule, now_ms)
        full_job = replace(
            job,
            state=replace(job.state, next_run_at_ms=next_ms),
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        self._jobs[job.id] = full_job
        self._save()
        logger.info("Job added: %s (%s)", job.id, job.name)

    def remove_job(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._save()
            return True
        return False

    def list_jobs(self) -> list[CronJob]:
        return list(self._jobs.values())

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        self._jobs[job_id] = replace(job, enabled=enabled)
        self._save()
        return True

    async def run_now(self, job_id: str) -> bool:
        """Fire a job immediately regardless of its schedule."""
        job = self._jobs.get(job_id)
        if job is None:
            return False

        now_ms = int(self._clock() * 1000)
        try:
            await self._on_job(job)
            status: Literal["ok", "error", "skipped"] = "ok"
            error: str | None = None
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error = str(exc)
            logger.error("run_now for job %s failed: %s", job_id, exc)

        record = CronRunRecord(run_at_ms=now_ms, status=status, error=error)
        history = (job.state.run_history + [record])[-_MAX_HISTORY:]
        next_ms = self._compute_next_run(job.schedule, now_ms)
        new_state = CronJobState(
            next_run_at_ms=next_ms,
            last_run_at_ms=now_ms,
            last_status=status,
            last_error=error,
            run_history=history,
        )
        self._jobs[job_id] = replace(job, state=new_state)
        self._save()
        return True

    def status(self) -> dict[str, Any]:
        return {"jobs": len(self._jobs), "running": self._running}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._store_path):
            self._jobs = {}
            return
        try:
            with open(self._store_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._jobs = {d["id"]: _job_from_dict(d) for d in data.get("jobs", [])}
            logger.info("Loaded %d jobs from %s", len(self._jobs), self._store_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load scheduler store: %s — starting empty", exc)
            self._jobs = {}

    def _save(self) -> None:
        store_dir = os.path.dirname(self._store_path)
        if store_dir:
            os.makedirs(store_dir, exist_ok=True)
        data = {"jobs": [_job_to_dict(job) for job in self._jobs.values()]}
        tmp_path = self._store_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, self._store_path)
