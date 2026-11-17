"""One-off resume: finish run v2-20260613-220815's assembly after a laptop
sleep timed out the Sync.so result download.

Reuses the 4 already-paid lipsync clips (beats 0,2,6,7 — their synced files
exist and are in manifest['lipsync']) and re-lipsyncs ONLY the 3 talking-head
beats whose download died (1,4,9). B-roll (3,5,8) re-trims locally. Then
concat + overlay the continuous VO. No Seedance re-render — the 10 clips are
reused as-is.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.assembly import assemble
from app.config import load_settings
from app.lipsync import make_lipsync_client
from app.uploader import make_uploader
from app.voice import Alignment

RUN = Path("jobs/workflows/v2-20260613-220815")


async def main() -> None:
    m = json.loads((RUN / "manifest.json").read_text())
    windows = [(w["start"], w["end"]) for w in m["beat_windows"]]
    beats_raw = m["timeline"]["beats"]
    beats = [
        SimpleNamespace(
            index=b["index"],
            shot_type=b["shot_type"],
            beat_kind=b.get("beat_kind", "speech"),
        )
        for b in beats_raw
    ]
    clip_paths = [next(c["local_path"] for c in m["clips"] if c["index"] == i)
                  for i in range(len(windows))]
    # assemble() only reads alignment.duration on the sync.so path (captions
    # off). Σ windows == VO duration by design, so the last window end is it.
    last_end = windows[-1][1]
    alignment = Alignment(characters=["x"], starts=[0.0], ends=[last_end])
    prior = m.get("lipsync", [])
    print(f"reusing {len(prior)} paid lipsync clips: "
          f"{sorted(r['index'] for r in prior)}")

    settings = load_settings()
    res = await assemble(
        beats=beats, windows=windows, clip_paths=clip_paths,
        vo_audio_path=str(RUN / "vo-paced.wav"), alignment=alignment,
        lipsync_client=make_lipsync_client(settings),
        uploader=make_uploader(settings),
        work_dir=RUN / "assembly", out_path=RUN / "final.mp4",
        native_lipsync=False, lipsync_concurrency=3,
        prior_lipsync_records=prior,
        on_asset=lambda k, p: print(f"  [{k}] beat {p.get('index')}"),
    )
    print(f"DONE → {res.path} ({res.duration_s:.1f}s, "
          f"{res.n_lipsynced} lipsynced)")


if __name__ == "__main__":
    asyncio.run(main())
