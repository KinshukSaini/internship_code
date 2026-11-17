"""Per-task JSON job log. One file per task_id."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobLog:
    def __init__(self, root: Path | str = "jobs"):
        self._root = Path(root)

    def _path(self, task_id: str) -> Path:
        return self._root / f"{task_id}.json"

    def _read(self, task_id: str) -> dict:
        return json.loads(self._path(task_id).read_text())

    def _write(self, task_id: str, data: dict) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._path(task_id).write_text(json.dumps(data, indent=2, default=str))

    def write_initial(
        self,
        *,
        task_id: str,
        prompt: str,
        assets: list[dict],
        settings: dict,
        request_payload: dict[str, Any],
    ) -> None:
        self._write(task_id, {
            "task_id": task_id,
            "created_at": _now_iso(),
            "prompt": prompt,
            "assets": assets,
            "settings": settings,
            "request": request_payload,
            "status_history": [],
            "result": None,
        })

    def append_status(self, task_id: str, status: str) -> None:
        data = self._read(task_id)
        data["status_history"].append({"at": _now_iso(), "status": status})
        self._write(task_id, data)

    def write_final(self, task_id: str, result: dict) -> None:
        data = self._read(task_id)
        data["result"] = result
        data["finished_at"] = _now_iso()
        self._write(task_id, data)
