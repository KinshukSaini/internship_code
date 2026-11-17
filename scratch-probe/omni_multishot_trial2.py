"""Trial 2: can a b-roll INSERT survive inside a multi-shot take?

Trial 1 proved Omni cuts on time (2.917s / 5.958s vs 3.0 / 6.0) and holds
identity, but shot 2 — "her hand pressing against her stomach" — came back as
another talking-to-lens angle. Two suspects, both changed here:

  1. The block said what to SHOW but never what to EXCLUDE. Now the insert names
     the exclusions (face out of frame, not speaking on camera) and carries a
     shot-type label, which is how a shot list actually reads.
  2. The audio clause said "the creator says out loud" for the WHOLE take, which
     tells the model she is on camera talking the entire time. Now the line is
     declared to continue as off-camera VOICEOVER during the insert.

Same take, same anchor, same duration, same line as trial 1 — so the frames are
directly comparable at 1.4s / 4.5s / 7.6s.
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
OUT = ROOT / "scratch-probe" / "omni-multishot-trial2.mp4"


def _load_env() -> None:
    import os
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    _load_env()
    from app.config import OMNI_EVOLINK_MODEL, load_settings
    from app.gemini import render_voice_persona
    from app.pipeline import (_ANATOMY_CLAUSE, _OMNI_CHAR_REF_CLAUSE,
                              _OMNI_LINE_FIDELITY_CLAUSE, _OMNI_META_CLAUSE,
                              _OMNI_NO_MUSIC_CLAUSE, _OMNI_NO_TEXT_CLAUSE,
                              _OMNI_REALISM_CLAUSE, _SKIN_TEXTURE_CLAUSE,
                              _OMNI_BRISK_DELIVERY_CLAUSE)
    from app.video_gen import EvoLinkVideoClient, VideoRequest

    data = json.loads(MANIFEST.read_text())
    persona = render_voice_persona(
        SimpleNamespace(**data["character"]["voice_persona"]), include_pace=True)
    anchor = next(a["url"] for a in data["anchors"]
                  if a["shot_type"] == "talking_head"
                  and a.get("framing") == "confessional")
    line = ("fungal invasion in the gut. Bloating, fatigue, acidity, even skin "
            "issues: the root cause")

    prompt = (
        f"Create a vertical 9:16 UGC social video. {_OMNI_CHAR_REF_CLAUSE}\n\n"
        f"3 shots in one continuous 9-second take:\n"
        # Shot 1 — unchanged from trial 1 (it worked).
        f"[0-3s] ON CAMERA, SPEAKING: the creator holds the phone at arm's "
        f"length in a handheld selfie framing, talking straight to the lens, "
        f"leaning slightly toward it with a serious look, one open palm "
        f"gesturing.\n"
        # Shot 2 — the shot that failed. Labelled, and the exclusions named.
        f"[3-6s] INSERT CUTAWAY, NOBODY SPEAKING ON CAMERA: a tight close-up of "
        f"her own hand pressing flat against her stomach over the sage tee, "
        f"framed from chest to waist ONLY. Her face, mouth and head are OUT OF "
        f"FRAME entirely for this whole shot — do not show her face, and do not "
        f"show anyone talking. Static close framing on the hand and the tee.\n"
        f"[6-9s] BACK ON CAMERA, SPEAKING: the camera is still, propped at eye "
        f"level, the creator talking straight to the lens with a steady "
        f"knowledgeable gaze, brows raised slightly.\n"
        f"Natural smartphone capture, real-time pace, never slow motion. "
        f"Follow the shot list exactly and in order, cutting at the given "
        f"times — each entry is its own scene, and the scene must genuinely "
        f"change at each cut, with a hard cut and no dissolve, wipe or "
        f"transition effect. Obey each shot's framing and exclusions exactly: a "
        f"shot marked INSERT CUTAWAY must NOT show her face. Keep the same "
        f"creator, wardrobe and grade throughout. Do not add any scene, "
        f"location, person or product that is not on the list.\n\n"
        # The audio fix: one unbroken read, but explicitly VOICEOVER over the
        # insert, so "she is speaking" stops implying "she is on camera".
        f"Audio: one unbroken take of speech across all three shots that never "
        f"restarts, pauses or repeats: \"{line}\". She is ON CAMERA speaking it "
        f"during shots 1 and 3; during the INSERT CUTAWAY shot her voice "
        f"CONTINUES as an off-camera voiceover over the cutaway while she is not "
        f"visible. {persona} {_OMNI_LINE_FIDELITY_CLAUSE} "
        f"{_OMNI_BRISK_DELIVERY_CLAUSE} {_OMNI_NO_MUSIC_CLAUSE}\n\n"
        f"{_SKIN_TEXTURE_CLAUSE} {_OMNI_REALISM_CLAUSE} {_OMNI_META_CLAUSE}\n\n"
        f"{_ANATOMY_CLAUSE} {_OMNI_NO_TEXT_CLAUSE}"
    )
    print("=" * 72 + f"\n{prompt}\n" + "=" * 72)

    client = EvoLinkVideoClient(load_settings())
    req = VideoRequest(
        prompt=prompt, first_frame_url=anchor, last_frame_url=None,
        duration=10, aspect_ratio="9:16", resolution="720p",
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
    print(f"downloaded → {OUT}")

    log = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(OUT), "-filter:v",
         "select='gt(scene,0.08)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    cuts = [round(float(p.split("pts_time:")[1].split()[0]), 2)
            for p in log.split("\n") if "pts_time:" in p]
    print(f"\ncuts detected at {cuts} (asked 3.0 / 6.0)")

    for t in ("1.4", "4.5", "7.6"):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-ss", t, "-i", str(OUT), "-frames:v", "1", "-q:v", "3",
                        str(ROOT / f"scratch-probe/t2_frame_{t}.jpg"), "-y"])
    print("frames → scratch-probe/t2_frame_{1.4,4.5,7.6}.jpg")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
