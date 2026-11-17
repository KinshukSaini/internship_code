"""Clone the deduped, selected voices into ElevenLabs and record their ids.

Reads data/voice_clones/voices.json and clones every record with
`selected == true` that doesn't already have an `elevenlabs_voice_id`
(so it's resumable / safe to re-run). The ORIGINAL staged sample is used
(remove_background_noise=True), labels carry the detected characteristics,
and the new voice_id + name are written back into voices.json.

    uv run python scripts/clone_selected.py
    uv run python scripts/clone_selected.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "data" / "voice_clones" / "voices.json"
STAGE_DIR = Path(tempfile.gettempdir()) / "clone_voices_stage"
CLONE_NAME_PREFIX = "OCLabs"
CONCURRENCY = 3

# ElevenLabs IVC caps sample upload size (~10 MB). Large exports (some YT files
# are 15-90 MB) 400 out, so trim to a bounded, clean window — a few minutes of
# speech is more than enough for a clone.
MAX_UPLOAD_BYTES = 9_000_000
CLONE_TRIM_S = 180


def _clone_name(rec: dict) -> str:
    m = re.match(r"\s*(\d+)", Path(rec["source_file"]).stem)
    slug = m.group(1) if m else str(rec.get("row", 0))
    return f"{CLONE_NAME_PREFIX}-{rec['platform']}-{slug}"


def _clone_audio(src: Path) -> bytes:
    """Bytes to upload: original if small enough, else a trimmed mono clip."""
    if src.stat().st_size <= MAX_UPLOAD_BYTES:
        return src.read_bytes()
    r = subprocess.run(
        ["ffmpeg", "-y", "-t", str(CLONE_TRIM_S), "-i", str(src),
         "-ac", "1", "-b:a", "128k", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True)
    return r.stdout


async def _clone_one(el_client, sem, rec: dict) -> tuple[dict, str | None]:
    src = STAGE_DIR / rec["platform"] / rec["source_file"]
    if not src.exists():
        return rec, f"missing staged file {src}"
    ch = rec.get("characteristics") or {}
    name = _clone_name(rec)
    labels = {k: v for k, v in {
        "language": ch.get("language"), "gender": ch.get("gender"),
        "accent": ch.get("accent_region"), "age": ch.get("age_band"),
    }.items() if v}
    try:
        audio = await asyncio.to_thread(_clone_audio, src)
        async with sem:
            resp = await el_client.voices.ivc.create(
                name=name,
                files=[(rec["source_file"], audio, "audio/mpeg")],
                remove_background_noise=True,
                description=(ch.get("summary") or "")[:500] or None,
                labels=json.dumps(labels),
            )
        vid = getattr(resp, "voice_id", None) or (
            resp.get("voice_id") if isinstance(resp, dict) else None)
        if not vid:
            rec["error"] = f"no voice_id in response: {resp!r}"
            return rec, rec["error"]
        rec["elevenlabs_voice_id"] = vid
        rec["elevenlabs_name"] = name
        rec["status"] = "done"
        rec["error"] = None
        return rec, None
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec, rec["error"]


async def _run(dry_run: bool) -> int:
    settings = load_settings()
    recs = json.loads(OUT_JSON.read_text())

    # A record needs cloning if selected AND its voice_id is absent OR no longer
    # exists in the account (ids from an earlier run were deleted in cleanup).
    from elevenlabs.client import AsyncElevenLabs, ElevenLabs
    live = {v.voice_id for v in ElevenLabs(
        api_key=settings.elevenlabs_api_key).voices.get_all().voices}
    todo = [r for r in recs if r.get("selected")
            and (r.get("elevenlabs_voice_id") not in live)]
    stale = [r for r in todo if r.get("elevenlabs_voice_id")]
    print(f"{sum(1 for r in recs if r.get('selected'))} selected; {len(todo)} to "
          f"clone ({len(stale)} had a deleted/stale id, "
          f"{len(todo) - len(stale)} never cloned).")
    if dry_run:
        for r in todo:
            print(f"  [dry-run] would clone {_clone_name(r)}  <- {r['key']}")
        return 0
    if not todo:
        print("Nothing to clone.")
        return 0

    el_client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
    sem = asyncio.Semaphore(CONCURRENCY)

    lock = asyncio.Lock()

    async def _worker(rec):
        rec, err = await _clone_one(el_client, sem, rec)
        async with lock:
            OUT_JSON.write_text(json.dumps(recs, indent=2, ensure_ascii=False))
        if err:
            print(f"  ERROR {rec['key']}: {err[:110]}")
        else:
            print(f"  cloned {rec['elevenlabs_name']:<18} {rec['elevenlabs_voice_id']}"
                  f"  <- {rec['key']}")

    await asyncio.gather(*[_worker(r) for r in todo])

    done = sum(1 for r in recs if r.get("selected") and r.get("elevenlabs_voice_id"))
    fail = sum(1 for r in recs if r.get("selected") and not r.get("elevenlabs_voice_id"))
    print(f"\nDone. cloned={done}, still-uncloned={fail}. Written → {OUT_JSON}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
