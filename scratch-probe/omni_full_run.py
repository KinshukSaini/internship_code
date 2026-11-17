"""Full Omni pipeline run on the REAL 2026-08-05 brief.

Exercises everything shipped 2026-08-05/06 in one pass:
  - the comma fix in _has_interior_phrase_boundary (the 8-word fast-cut cap now
    actually fires, so beats arrive ~3-4s instead of 5-10s);
  - the shot ceiling + speech headroom (no 10s shots, no mid-sentence cuts);
  - in-clip multi-shot takes with the trial-2 verified wording;
  - product_in_frame + the widened product-reference gate;
  - the EvoLink-Omni fallback for any clip Google refuses.

Uses the real product photo, so the product path is live too.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings          # noqa: E402
from app.pipeline import generate_ugc_video   # noqa: E402

MANIFEST = Path("/Users/harshkedia/Downloads/manifest.json")


async def main() -> None:
    data = json.loads(MANIFEST.read_text())
    brief = data["brief"]
    params = data["params"]

    settings = load_settings()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = ROOT / "jobs" / "workflows" / f"omni-fastcut-{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    def progress(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    result = await generate_ugc_video(
        brief=brief,
        settings=settings,
        work_dir=work,
        out_path=work / "final.mp4",
        video_generator="omni",
        product_ref_url=params.get("product_ref_url"),
        clip_resolution=params.get("clip_resolution", "720p"),
        burn_captions=False,          # no captions on this deliverable
        on_progress=progress,
    )
    print(f"\nDONE → {result}")


if __name__ == "__main__":
    asyncio.run(main())
