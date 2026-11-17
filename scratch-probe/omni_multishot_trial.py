"""Live trial: does Omni honour a timecode shot list inside ONE generation?

Orders exactly ONE clip (~$0.87 on EvoLink, since Gemini credits are depleted)
built the way the pipeline would build it: take 10 of the 2026-08-05 script —
three real screenplay scene changes over one continuous line.

Verifies objectively rather than by vibes:
  - ffmpeg scene detection: did ~3 cuts actually land, and roughly on the asked
    timings? (the whole premise — if Omni ignores the list we get one long shot)
  - silencedetect: is the speech continuous, or does it restart/pause per shot?
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST = Path("/Users/harshkedia/Downloads/manifest.json")
OUT = ROOT / "scratch-probe" / "omni-multishot-trial.mp4"


def _load_env() -> None:
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            import os
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _ffmpeg(*args: str) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", *args],
                          capture_output=True, text=True).stderr


async def main() -> int:
    _load_env()
    from app.config import OMNI_EVOLINK_MODEL, load_settings
    from app.gemini import render_voice_persona
    from app.pipeline import (_beat_uses_product, _fallback_prompt,
                              _omni_duration, _omni_windows)
    from app.video_gen import EvoLinkVideoClient, VideoRequest

    data = json.loads(MANIFEST.read_text())
    product = data["params"]["product_ref_url"]
    beats = [SimpleNamespace(**b) for b in data["beats"]]
    out, wins, _ = _omni_windows(beats, product_ref_url=product,
                                 on_progress=lambda m: None)

    # The richest fold: most shots, no product beat in it (a product shot needs
    # the product reference image, and EvoLink's route carries exactly one).
    idx = max(
        (i for i, b in enumerate(out)
         if len(getattr(b, "shots", None) or []) > 1
         and not any(_beat_uses_product(s, product) for s in b.shots)),
        key=lambda i: len(out[i].shots),
    )
    take, (s0, s1) = out[idx], wins[idx]
    window = s1 - s0
    duration = _omni_duration(window)

    persona = render_voice_persona(
        SimpleNamespace(**data["character"]["voice_persona"]), include_pace=True)
    prompt = _fallback_prompt(take, window, lipsync_engine="syncso",
                              omni_mode=True, persona_clause=persona)

    # Shot 1's anchor: the rendered frame the take opens on. Matched on the
    # screenplay fields so the trial uses a REAL anchor from this run.
    first = take.shots[0]
    anchor = next(
        a["url"] for a in data["anchors"]
        if a["shot_type"] == first.shot_type
        and a.get("framing") == getattr(first, "framing", None)
    )

    print(f"take {idx}: {len(take.shots)} shots / {window:.1f}s window / "
          f"ordering {duration}s (~${duration * 0.087:.2f})")
    print(f"anchor: {anchor}\n")
    print("=" * 72)
    print(prompt)
    print("=" * 72)

    settings = load_settings()
    client = EvoLinkVideoClient(settings)
    req = VideoRequest(
        prompt=prompt, first_frame_url=anchor, last_frame_url=None,
        duration=duration, aspect_ratio="9:16", resolution="720p",
        generate_audio=True, seed=None, model=OMNI_EVOLINK_MODEL,
    )
    try:
        task_id, _ = await client.create_task_async(req)
        print(f"\nordered: {task_id}")
        url = None
        async for status, res in client.poll_task_stream_async(task_id):
            print(f"  {status}", flush=True)
            if res is not None:
                url = res.video_url
    finally:
        await client.close()
    if not url:
        print("no artifact")
        return 1

    from app.stitcher import download
    await asyncio.to_thread(download, url, OUT)
    print(f"\ndownloaded → {OUT}")

    # ── verification ────────────────────────────────────────────────
    print("\n--- CUTS (scene score > 0.3) ---")
    log = _ffmpeg("-i", str(OUT), "-filter:v",
                  "select='gt(scene,0.3)',showinfo", "-f", "null", "-")
    cuts = [round(float(p.split("pts_time:")[1].split()[0]), 2)
            for p in log.split("\n") if "pts_time:" in p]
    print(f"detected {len(cuts)} cut(s) at {cuts}")
    step = window / len(take.shots)
    print(f"asked for {len(take.shots) - 1} cut(s) near "
          f"{[round(step * j, 1) for j in range(1, len(take.shots))]}")

    print("\n--- SPEECH CONTINUITY (silences > 0.4s) ---")
    log = _ffmpeg("-i", str(OUT), "-af",
                  "silencedetect=noise=-30dB:d=0.4", "-f", "null", "-")
    gaps = [ln.strip() for ln in log.split("\n") if "silence_" in ln]
    print("\n".join(gaps) or "no silence gaps — one continuous read")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
