"""
agents/openai_agent.py — Simple agent backed by OpenAI ChatGPT.

Conversation history is stored as JSON files in sessions_dir so sessions
can be resumed across jobs.  Each session file is:
    <sessions_dir>/<session_id>.json  →  list[{"role": str, "content": str}]
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid

from app.models import RunResult

logger = logging.getLogger(__name__)


class OpenAIAgent:
    """
    Calls the OpenAI Chat Completions API (with streaming) and maintains
    per-session conversation history on disk.

    Usage:
        agent = OpenAIAgent(api_key="sk-...", model="gpt-4o-mini", sessions_dir="/data/sessions")
        result = agent.run(prompt="hello", cwd="/tmp", session_id=None, job_id="abc")
    """

    name = "simple"

    def __init__(self, api_key: str, model: str, sessions_dir: str) -> None:
        import openai  # lazy import so missing package only errors when agent is used

        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self._sessions_dir = sessions_dir
        # job_id (or session_id fallback) → cancel flag
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        cwd: str,
        session_id: str | None = None,
        job_id: str | None = None,
    ) -> RunResult:
        sid = session_id or str(uuid.uuid4())
        key = job_id or sid

        history = self._load_history(sid)
        history = history + [{"role": "user", "content": prompt}]

        with self._lock:
            if key in self._cancelled:
                self._cancelled.discard(key)
                return RunResult(
                    session_id=sid,
                    stdout="",
                    stderr="Cancelled before start",
                    exit_code=1,
                    summary="(cancelled)",
                )

        logger.info(
            "OpenAI request: model=%s session=%s history_len=%d",
            self._model,
            sid,
            len(history),
        )

        response_text = ""
        try:
            stream = self._client.chat.completions.create(
                model=self._model,
                messages=history,  # type: ignore[arg-type]
                stream=True,
            )
            for chunk in stream:
                with self._lock:
                    if key in self._cancelled:
                        self._cancelled.discard(key)
                        logger.info("OpenAI stream cancelled (job=%s)", key)
                        break
                delta = chunk.choices[0].delta.content or ""
                response_text += delta
        except Exception as exc:
            logger.error("OpenAI call failed (job=%s): %s", key, exc)
            return RunResult(
                session_id=sid,
                stdout="",
                stderr=str(exc),
                exit_code=1,
                summary=f"OpenAI error: {exc}",
            )

        updated_history = history + [{"role": "assistant", "content": response_text}]
        self._save_history(sid, updated_history)

        summary = response_text[:3000] + ("\n… (truncated)" if len(response_text) > 3000 else "")
        return RunResult(
            session_id=sid,
            stdout=response_text,
            stderr="",
            exit_code=0,
            summary=summary,
        )

    def cancel(self, job_id: str) -> None:
        logger.info("Cancel requested for job %s (OpenAI agent)", job_id)
        with self._lock:
            self._cancelled.add(job_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_history(self, session_id: str) -> list[dict]:
        path = os.path.join(self._sessions_dir, f"{session_id}.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
                return list(data)
        except json.JSONDecodeError as exc:
            logger.error("Corrupt session history %s (will start fresh): %s", session_id, exc)
            return []
        except OSError as exc:
            logger.warning("Failed to load session history %s: %s", session_id, exc)
            return []

    def _save_history(self, session_id: str, history: list[dict]) -> None:
        os.makedirs(self._sessions_dir, exist_ok=True)
        path = os.path.join(self._sessions_dir, f"{session_id}.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(history, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("Failed to save session history %s: %s", session_id, exc)
