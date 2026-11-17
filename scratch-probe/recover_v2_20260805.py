"""Recover the 2026-08-05 Omni run: 12 clips free, beats 0/3/9 re-ordered."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.pipeline import resume_ugc_video

RUN = "jobs/workflows/v2-20260805-152118"

async def main():
    s = load_settings()
    res = await resume_ugc_video(
        RUN, settings=s, out_path=f"{RUN}/final.mp4",
        allow_paid_reorder=True,
        # No captions on this deliverable (explicit request), so the force-align
        # pass is skipped outright rather than left to fail open on a
        # libass-less ffmpeg.
        burn_captions=False,
        on_progress=lambda m: print(m, flush=True),
    )
    print("\n=== RESULT ===")
    print("out:", getattr(res, "out_path", None) or getattr(res, "video_path", None))
    for attr in ("duration_s", "clip_paths", "cost", "captions_burned"):
        if hasattr(res, attr):
            print(f"{attr}: {getattr(res, attr)}")

asyncio.run(main())
