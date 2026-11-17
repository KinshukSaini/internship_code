"""End-to-end smoke test of the Phase-4 UGC pipeline.

Runs the full headless pipeline on one brief:
  plan_timeline → VO (ElevenLabs) → anchors (Nano Banana) → clips (Seedance
  single-image I2V) → assemble (trim → lipsync → concat → VO → captions).

Requires .env: EVOLINK_API_KEY, GEMINI_API_KEY, ELEVENLABS_API_KEY,
SYNC_API_KEY, AWS_* . Costs ~$2-4 (mostly Sync.so lipsync).

Usage:
    uv run python scripts/test_pipeline.py
    uv run python scripts/test_pipeline.py --duration 45 --voice <voice_id>

If --voice is omitted, the first premade Indian-English voice is used.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from app.config import load_settings
from app.pipeline import generate_ugc_video

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BRIEF = (
    "UGC ad for a vitamin C face serum aimed at urban Indian women in their "
    "20s. Casual, authentic, talking-head + product demo. The creator shares "
    "how it faded her dark spots in 3 weeks. One CTA: link in bio."
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Phase-4 pipeline smoke test")
    parser.add_argument("--duration", type=int, default=45)
    parser.add_argument("--voice", default=None,
                        help="ElevenLabs voice_id override (default: auto-"
                             "picked by character via voice_vibe catalog)")
    parser.add_argument("--brief", default=DEFAULT_BRIEF)
    args = parser.parse_args()

    settings = load_settings()
    voice_id = args.voice   # None → pipeline picks from character's voice_vibe

    ts = time.strftime("%Y%m%d-%H%M%S")
    work = ROOT / "jobs" / "workflows" / f"pipeline-{ts}"
    out = work / "final.mp4"

    result = await generate_ugc_video(
        brief=args.brief, target_duration_s=args.duration, voice_id=voice_id,
        settings=settings, work_dir=work, out_path=out,
        on_progress=lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}"),
    )

    print("\n=== RESULT ===")
    print(f"final video : {result.final_video_path}")
    print(f"duration    : {result.duration_s:.1f}s")
    print(f"lipsynced   : {result.n_lipsynced} beats")
    print(f"character   : {result.character_ref_url}")


if __name__ == "__main__":
    asyncio.run(main())
