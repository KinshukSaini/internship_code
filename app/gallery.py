"""Local JSON-backed image gallery. Append-only with idempotent add."""
from __future__ import annotations

import json
from pathlib import Path

from app.image_gen import GeneratedImage


class Gallery:
    def __init__(self, index_path: Path | str = "jobs/images.json"):
        self._path = Path(index_path)

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return []

    def _write(self, data: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))

    def add(self, img: GeneratedImage) -> None:
        data = self._read()
        if any(d.get("id") == img.id for d in data):
            return
        data.append(img.to_dict())
        self._write(data)

    def list(self, *, source: str | None = None,
             limit: int = 500) -> list[GeneratedImage]:
        rows = list(reversed(self._read()))
        if source is not None:
            rows = [d for d in rows if d.get("source") == source]
        return [GeneratedImage.from_dict(d) for d in rows[:limit]]

    def get(self, image_id: str) -> GeneratedImage | None:
        for d in self._read():
            if d.get("id") == image_id:
                return GeneratedImage.from_dict(d)
        return None
