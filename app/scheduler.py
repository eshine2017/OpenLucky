"""
scheduler.py — In-daemon cron scheduler.

Runs on PTB's asyncio event loop. All state lives on that loop; command
handlers that mutate scheduler state must hop via
asyncio.run_coroutine_threadsafe(coro, loop).

Two separate files:
  spec_path  = <workspace>/cron.json     (user/Claude editable job definitions)
  state_path = <data>/cron-state.json    (daemon-owned runtime state)
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
class CronJob:
    id: str
    name: str
    enabled: bool
    cron_expr: str
    tz: str
    prompt: str
    state: CronJobState = field(default_factory=CronJobState)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _compute_next_run(cron_expr: str, tz_name: str, now_ms: int) -> int | None:
    """
    Compute the next firing time for a 5-field cron expression.

    Returns None on invalid cron expression (logs error).
    Falls back to UTC on invalid timezone.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from croniter import croniter  # type: ignore[import-untyped]

    # Resolve timezone
    try:
        tz = ZoneInfo(tz_name) if tz_name else UTC
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("Invalid timezone %r — falling back to UTC", tz_name)
        tz = UTC

    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=tz)

    try:
        cron = croniter(cron_expr, now_dt)
        next_dt: datetime = cron.get_next(datetime)
        return int(next_dt.timestamp() * 1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("Invalid cron expression %r: %s", cron_expr, exc)
        return None


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


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    def __init__(
        self,
        spec_path: str,
        state_path: str,
        on_job: Callable[[CronJob], Awaitable[None]],
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._spec_path = spec_path
        self._state_path = state_path
        self._on_job = on_job
        self._clock = clock or time.time

        # Spec: list of CronJob (no state embedded)
        self._spec: list[CronJob] = []
        self._spec_mtime: float = 0.0

        # State: job_id → CronJobState
        self._state: dict[str, CronJobState] = {}

        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None  # set by main.py post_init

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load spec, load state, recompute next_run for all, then arm the timer."""
        self._load_spec()
        self._load_state()

        # Recompute from now so a daemon restart never fires a burst of missed runs.
        now_ms = int(self._clock() * 1000)
        for job in self._spec:
            next_ms = _compute_next_run(job.cron_expr, job.tz, now_ms)
            state = self._state.get(job.id, CronJobState())
            self._state[job.id] = replace(state, next_run_at_ms=next_ms)
        self._save_state()

        self._running = True
        self._arm_timer()
        logger.info("Scheduler started with %d jobs", len(self._spec))

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

        for job in self._spec:
            if not job.enabled:
                continue
            state = self._state.get(job.id)
            if state is None or state.next_run_at_ms is None:
                continue
            if due_ms is None or state.next_run_at_ms < due_ms:
                due_ms = state.next_run_at_ms

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
        self._reload_spec_if_changed()

        now_ms = int(self._clock() * 1000)

        due = [
            job
            for job in self._spec
            if job.enabled
            and (s := self._state.get(job.id)) is not None
            and s.next_run_at_ms is not None
            and s.next_run_at_ms <= now_ms
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

            old_state = self._state.get(job.id, CronJobState())
            record = CronRunRecord(run_at_ms=now_ms, status=status, error=error)
            history = (old_state.run_history + [record])[-_MAX_HISTORY:]
            next_ms = _compute_next_run(job.cron_expr, job.tz, now_ms)
            self._state[job.id] = CronJobState(
                next_run_at_ms=next_ms,
                last_run_at_ms=now_ms,
                last_status=status,
                last_error=error,
                run_history=history,
            )

        self._save_state()

        if self._running:
            self._arm_timer()

    # ------------------------------------------------------------------
    # Public job management
    # ------------------------------------------------------------------

    def list_jobs(self) -> list[CronJob]:
        """Return jobs from spec with current state merged in."""
        return [
            replace(job, state=self._state.get(job.id, CronJobState()))
            for job in self._spec
        ]

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        """Update enabled flag in spec file and in-memory."""
        if not any(j.id == job_id for j in self._spec):
            return False

        # Read, update, write atomically
        try:
            with open(self._spec_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to read spec for set_enabled: %s", exc)
            return False

        for entry in data.get("jobs", []):
            if entry.get("id") == job_id:
                entry["enabled"] = enabled

        self._atomic_write_json(self._spec_path, data)
        self._load_spec()
        return True

    def remove_job(self, job_id: str) -> bool:
        """Remove job from spec file and state. Updates in-memory immediately."""
        if not any(j.id == job_id for j in self._spec):
            return False

        # Update spec file
        try:
            with open(self._spec_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to read spec for remove_job: %s", exc)
            return False

        data["jobs"] = [e for e in data.get("jobs", []) if e.get("id") != job_id]
        self._atomic_write_json(self._spec_path, data)

        # Remove from state
        self._state.pop(job_id, None)
        self._save_state()

        # Update in-memory spec
        self._spec = [j for j in self._spec if j.id != job_id]
        return True

    async def run_now(self, job_id: str) -> bool:
        """Fire a job immediately regardless of its schedule."""
        self._reload_spec_if_changed()

        job = next((j for j in self._spec if j.id == job_id), None)
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

        old_state = self._state.get(job_id, CronJobState())
        record = CronRunRecord(run_at_ms=now_ms, status=status, error=error)
        history = (old_state.run_history + [record])[-_MAX_HISTORY:]
        next_ms = _compute_next_run(job.cron_expr, job.tz, now_ms)
        self._state[job_id] = CronJobState(
            next_run_at_ms=next_ms,
            last_run_at_ms=now_ms,
            last_status=status,
            last_error=error,
            run_history=history,
        )
        self._save_state()
        return True

    # ------------------------------------------------------------------
    # Spec loading
    # ------------------------------------------------------------------

    def _load_spec(self) -> None:
        """Load job definitions from spec_path. Tracks mtime."""
        if not os.path.exists(self._spec_path):
            self._spec = []
            self._spec_mtime = 0.0
            return
        try:
            mtime = os.path.getmtime(self._spec_path)
            with open(self._spec_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._spec = [
                CronJob(
                    id=e["id"],
                    name=e.get("name", e["id"]),
                    enabled=e.get("enabled", True),
                    cron_expr=e.get("cron_expr", "0 8 * * *"),
                    tz=e.get("tz", "UTC"),
                    prompt=e.get("prompt", ""),
                )
                for e in data.get("jobs", [])
            ]
            self._spec_mtime = mtime
            logger.info("Loaded %d jobs from spec %s", len(self._spec), self._spec_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load spec: %s — keeping current spec", exc)

    def _reload_spec_if_changed(self) -> None:
        """Reload spec if the file's mtime has changed."""
        if not os.path.exists(self._spec_path):
            return
        try:
            mtime = os.path.getmtime(self._spec_path)
            if mtime != self._spec_mtime:
                logger.info("Spec file changed; reloading")
                self._load_spec()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # State loading / saving
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load runtime state from state_path."""
        if not os.path.exists(self._state_path):
            self._state = {}
            return
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._state = {job_id: _state_from_dict(s) for job_id, s in data.items()}
            logger.info("Loaded state for %d jobs from %s", len(self._state), self._state_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load state: %s — starting empty", exc)
            self._state = {}

    def _save_state(self) -> None:
        """Atomically write runtime state to state_path."""
        state_dir = os.path.dirname(self._state_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        data = {job_id: _state_to_dict(s) for job_id, s in self._state.items()}
        self._atomic_write_json(self._state_path, data)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_write_json(path: str, data: object) -> None:
        dir_part = os.path.dirname(path)
        if dir_part:
            os.makedirs(dir_part, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, path)
