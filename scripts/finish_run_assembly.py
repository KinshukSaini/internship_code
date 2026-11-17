"""Manually finish a crashed heygen_replicate run: reuse the already-paid
clips + per-beat audio in a job dir, run the real assemble() (Replicate HeyGen
lipsync → concat, audio baked in), and write final.mp4.

This drives the SAME app.assembly.assemble() the pipeline uses — no
reimplementation of the stitch logic — feeding it inputs rebuilt from the
run's manifest.json. Idempotent-ish: re-running re-does the lipsync unless a
prior lipsync record is present.

Usage:
    uv run python scripts/finish_run_assembly.py jobs/workflows/v2-YYYYMMDD-HHMMSS
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Running `python scripts/x.py` puts scripts/ on sys.path, not the repo root —
# add the repo root so `import app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.assembly import assemble
from app.config import PHONE_CAPTURE_GRADE, REPLICATE_MAX_CONCURRENCY, load_settings
from app.lipsync import make_lipsync_client
from app.uploader import make_uploader


async def main(run_dir: Path) -> None:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    params = manifest.get("params", {})
    engine = params.get("lipsync_engine", "heygen_replicate")
    if engine != "heygen_replicate":
        print(f"WARNING: run engine is {engine!r}, not heygen_replicate — "
              f"this driver targets the replicate path.", file=sys.stderr)

    # Beats → objects (assemble reads attributes: shot_type, voice_mode,
    # dialogue_text, beat_kind, …). The manifest dicts carry exactly those.
    beats = [SimpleNamespace(**b) for b in manifest["timeline"]["beats"]]
    windows = [(w["start"], w["end"]) for w in manifest["beat_windows"]]

    # Clips ordered by beat index (they're already downloaded locally).
    clips_by_index = {c["index"]: c for c in manifest["clips"]}
    clip_paths = [clips_by_index[i]["local_path"] for i in range(len(beats))]

    beat_audio_paths = manifest["beat_audio_paths"]
    beat_audio_durs = manifest["beat_audio_durs"]
    vo_audio_path = manifest["vo_audio_path"]

    # The replicate path (burn_captions=False) only reads alignment.duration
    # (drift-check log + returned duration) — a light stub is sufficient.
    alignment = SimpleNamespace(
        duration=float(manifest["vo_duration_s"]),
        characters=[], starts=[], ends=[],
    )

    # Every referenced local file must exist before we spend on lipsync.
    missing = [p for p in clip_paths + beat_audio_paths + [vo_audio_path]
               if not Path(p).exists()]
    if missing:
        raise SystemExit(f"missing local artifacts, cannot finish: {missing}")

    settings = load_settings()
    uploader = make_uploader(settings)
    lipsync_client = make_lipsync_client(settings, engine)
    out_path = run_dir / "final.mp4"

    def _log(msg: str) -> None:
        print(f"[finish] {msg}", file=sys.stderr, flush=True)

    _log(f"assembling {len(beats)} beats via {engine} "
         f"(concurrency={REPLICATE_MAX_CONCURRENCY}) → {out_path}")
    result = await assemble(
        beats=beats, windows=windows, clip_paths=clip_paths,
        vo_audio_path=vo_audio_path, alignment=alignment,
        lipsync_client=lipsync_client, uploader=uploader,
        work_dir=run_dir / "assembly", out_path=out_path,
        native_lipsync=False, replicate_lipsync=True,
        beat_audio_paths=beat_audio_paths, beat_audio_durs=beat_audio_durs,
        lipsync_concurrency=REPLICATE_MAX_CONCURRENCY,
        prior_lipsync_records=manifest.get("lipsync"),
        clip_resolution=params.get("clip_resolution", "720p"),
        phone_capture_grade=PHONE_CAPTURE_GRADE,
    )
    _log(f"DONE → {result.path} ({result.duration_s:.1f}s, "
         f"{result.n_beats} beats, {result.n_lipsynced} lipsynced)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: finish_run_assembly.py <run_dir>")
    asyncio.run(main(Path(sys.argv[1])))
