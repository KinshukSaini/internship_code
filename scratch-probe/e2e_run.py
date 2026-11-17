"""E2E pipeline run — exercises the dev-reported fixes on a real brief.

Usage: .venv/bin/python scratch-probe/e2e_run.py seedance|omni
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings          # noqa: E402
from app.pipeline import generate_ugc_video   # noqa: E402

# Deliberately shaped like the dev's failing brief: production labels +
# quoted lines (exercises extraction sanitization + verbatim enforcement),
# plus explicit visual directives (exercises the directives threading).
BRIEF = """\
Ad brief for GlowPop vitamin-C face serum (no product photo for this test).
Vibe: fresh morning energy, honest best-friend recommendation.
Model: female, mid-20s, Mumbai creator.
Important visuals: open on the balcony in soft morning light; include one \
close-up b-roll of her applying serum to her cheek.

SCRIPT:
HOOK VO : "People always ask me how my skin looks this bright."
BODY : "It's this vitamin C serum. Two drops every morning."
BODY : "No stickiness, no white cast. Just glow."
CTA : "Try it for two weeks. You'll see."
"""


async def main(generator: str) -> None:
    settings = load_settings()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = ROOT / "jobs" / "workflows" / f"e2e-{generator}-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    out = work / "final.mp4"

    def progress(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    result = await generate_ugc_video(
        brief=BRIEF,
        settings=settings,
        work_dir=work,
        out_path=out,
        video_generator=generator,
        on_progress=progress,
    )
    print(f"\nDONE → {result}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "seedance"))
