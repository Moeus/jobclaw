"""Apply history tracker to prevent duplicate applications."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jobclaw.config import STATE_DIR

logger = logging.getLogger(__name__)
_BOSS_TIMEZONE = timezone(timedelta(hours=8))


class ApplyHistory:
    """Track Boss contact attempts to enforce a conservative daily cap.

    Schema:
        {
            "jobs": {
                "<job_id>": {
                    "status": "attempted | submitted",
                    "applied_at": "2026-03-04T05:00:00+00:00",
                    "source": "boss",
                    "query": "LLM实习生"
                }
            }
        }
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else STATE_DIR / "apply_history.json"
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """Load history from disk, or return empty structure."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                raise RuntimeError(
                    f"Cannot read application history at {self._path}; "
                    "stopping to protect the daily limit"
                ) from e
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
                raise RuntimeError(f"Invalid application history at {self._path}")
            return data
        return {"jobs": {}}

    def _save(self) -> None:
        """Persist history to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_name(self._path.name + ".tmp")
        temporary_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self._path)

    def is_applied(self, job_id: str) -> bool:
        """Skip jobs already contacted or attempted in a previous run."""
        return job_id in self._data.get("jobs", {})

    def mark_applied(
        self,
        job_id: str,
        status: str,
        source: str = "boss",
        query: str | None = None,
    ) -> None:
        """Record an application attempt."""
        previous = self._data.setdefault("jobs", {}).get(job_id, {})
        applied_at = (
            previous.get("applied_at")
            if previous.get("status") == "attempted" and status == "submitted"
            else None
        )
        self._data.setdefault("jobs", {})[job_id] = {
            "status": status,
            "applied_at": applied_at or datetime.now(timezone.utc).isoformat(),
            "source": source,
            "query": query if query is not None else previous.get("query"),
        }
        self._save()
        logger.debug("Marked job %s as %s", job_id, status)

    def today_count(self, query: str | None = None) -> int:
        """Count today's recorded contact attempts in Boss's China time zone."""
        today = datetime.now(_BOSS_TIMEZONE).date()
        count = 0
        for entry in self._data.get("jobs", {}).values():
            if entry.get("source") != "boss" or entry.get("status") not in {
                "attempted", "submitted",
            }:
                continue
            if query is not None and entry.get("query") != query:
                continue
            try:
                applied_at = datetime.fromisoformat(entry["applied_at"])
                if applied_at.tzinfo is None:
                    applied_at = applied_at.replace(tzinfo=timezone.utc)
                if applied_at.astimezone(_BOSS_TIMEZONE).date() == today:
                    count += 1
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid application timestamp in {self._path}; "
                    "stopping to protect the daily limit"
                ) from exc
        return count
