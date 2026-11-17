"""Download ElevenLabs voices (remixes) preview audio to voice_samples/.

Usage:
    .venv/bin/python scripts/fetch_eleven_remixes.py <voice_id> [<voice_id> ...]

Reads ELEVENLABS_API_KEY from .env. Saves each voice's preview sample as
voice_samples/elevenlabs_remixes/NN_<slug>__<voice_id>.mp3 and writes an
index.json with the metadata. The API key is never printed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
import os

DEST = Path("voice_samples/elevenlabs_remixes")
BASE = "https://api.elevenlabs.io/v1/voices"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "voice").strip()).strip("_").lower()
    return s or "voice"


def main(voice_ids: list[str]) -> int:
    load_dotenv(override=False)
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        print("ELEVENLABS_API_KEY not set in .env", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    headers = {"xi-api-key": key}
    index: list[dict] = []

    with httpx.Client(timeout=60) as client:
        for i, vid in enumerate(voice_ids, start=1):
            try:
                r = client.get(f"{BASE}/{vid}", headers=headers)
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                print(f"  [{vid}] FAILED to fetch metadata: "
                      f"HTTP {e.response.status_code} {e.response.text[:120]}")
                continue
            meta = r.json()
            name = meta.get("name") or vid
            preview = meta.get("preview_url")
            slug = _slug(name)
            entry = {
                "index": i, "voice_id": vid, "name": name,
                "category": meta.get("category"),
                "labels": meta.get("labels"),
                "description": meta.get("description"),
            }

            if not preview:
                print(f"  [{vid}] '{name}' — no preview_url on this voice; "
                      f"metadata saved only")
                index.append(entry)
                continue

            try:
                pr = client.get(preview, headers=headers)
                pr.raise_for_status()
            except httpx.HTTPStatusError as e:
                print(f"  [{vid}] '{name}' — preview download failed: "
                      f"HTTP {e.response.status_code}")
                index.append(entry)
                continue

            out = DEST / f"{i:02d}_{slug}__{vid}.mp3"
            out.write_bytes(pr.content)
            entry["file"] = out.name
            entry["bytes"] = len(pr.content)
            index.append(entry)
            print(f"  [{vid}] '{name}' → {out}  ({len(pr.content)//1024} KB)")

    (DEST / "index.json").write_text(json.dumps(index, indent=2))
    print(f"\nSaved {sum(1 for e in index if e.get('file'))}/"
          f"{len(voice_ids)} previews to {DEST}/  (+ index.json)")
    return 0


if __name__ == "__main__":
    ids = sys.argv[1:]
    if not ids:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(ids))
