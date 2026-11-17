"""Fresh, controlled HeyGen lipsync test isolating FRAMING.

Generates two Seedance talking-head clips of the SAME line-agnostic motion —
one CLOSE-UP (face fills frame) and one GENERAL (medium shot, like the
pipeline) — plus one fresh ElevenLabs line, then lipsyncs BOTH via HeyGen
(Replicate) exactly as the pipeline does (mux audio for the speaker gate).
Emits before/after clips + frames so we can see which framing HeyGen syncs.

Usage: uv run python scripts/rca_lipsync_framing.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.assembly import attach_audio, pad_audio_to
from app.config import load_settings
from app.image_gen import make_image_client
from app.lipsync import make_lipsync_client
from app.stitcher import download
from app.uploader import make_uploader
from app.video_gen import VideoRequest, make_video_client
from app.voice import make_voice_client, voice_for

OUT = Path("jobs/lipsync-framing")
LINE = "Wow, oh my — this is absolutely, totally mind blowing, you have to see it!"

ANCHORS = {
    "close": (
        "Vertical 9:16 close-up portrait photo of a young woman's face filling "
        "most of the frame, head and shoulders only, looking straight into the "
        "phone camera, relaxed natural expression, mouth slightly parted as if "
        "mid-sentence, soft natural daylight, shallow depth of field, realistic "
        "skin texture, casual selfie vlog look."
    ),
    "general": (
        "Vertical 9:16 medium shot photo of a young woman standing outdoors on a "
        "sunny street, waist-up, an SUV and trees blurred behind her, looking "
        "into the phone camera, casual outfit, natural daylight, realistic "
        "candid UGC selfie-vlog framing — her face is a smaller part of the frame."
    ),
}
MOTION = ("She speaks directly to the camera, mouth clearly opening and closing "
          "through the whole clip as she talks, natural micro head movements and "
          "blinks. Static locked-off camera, no zoom.")


def _probe_dur(p) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(p)], capture_output=True, text=True
    ).stdout.strip())


def _frames(src, tag):
    for t in ("0.8", "1.6", "2.4", "3.2"):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", t,
                        "-i", str(src), "-frames:v", "1",
                        str(OUT / f"frame_{tag}_{t}.png"), "-y"], check=True)


async def _seedance(video_client, anchor_url: str, tag: str) -> Path:
    req = VideoRequest(
        prompt=MOTION, first_frame_url=anchor_url, last_frame_url=None,
        duration=5, aspect_ratio="9:16", resolution="720p",
        generate_audio=False, seed=12345, camera_fixed=True, audio_url=None,
    )
    task_id, _ = await video_client.create_task_async(req)
    final = None
    async for _st, resp in video_client.poll_task_stream_async(task_id):
        if resp is not None:
            final = resp
    if final is None:
        raise RuntimeError(f"{tag}: seedance produced no clip")
    dst = OUT / f"clip_{tag}.mp4"
    download(final.video_url, dst)
    print(f"[{tag}] seedance clip → {dst} ({_probe_dur(dst):.2f}s)")
    return dst


async def _heygen(client, uploader, clip: Path, target: Path, tag: str) -> Path:
    clip_dur = _probe_dur(clip)
    padded = OUT / f"audio_{tag}_padded.wav"
    pad_audio_to(target, clip_dur, padded)
    muxed = OUT / f"muxed_{tag}.mp4"
    attach_audio(clip, padded, muxed, clip_dur)           # speaker gate
    before = OUT / f"BEFORE_{tag}.mp4"
    attach_audio(clip, padded, before, clip_dur)          # source + audio, no sync
    vurl = uploader.upload(str(muxed))
    aurl = uploader.upload(str(padded))
    res = await client.lipsync(video_url=vurl, audio_url=aurl,
                               output_file_name=f"framing-{tag}")
    after = OUT / f"AFTER_{tag}.mp4"
    download(res.output_url, after)
    _frames(clip, f"{tag}_source")
    _frames(after, f"{tag}_synced")
    print(f"[{tag}] HeyGen → {after}  (before: {before})")
    return after


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    uploader = make_uploader(s)
    image_client = make_image_client(s, uploader)
    video_client = make_video_client(s)
    voice = make_voice_client(s)
    lip = make_lipsync_client(s, "heygen_replicate")

    # 1. Fresh audio line.
    raw = OUT / "target-raw.mp3"
    vo = await voice.generate_vo(
        script=LINE, voice_id=voice_for("female", "sassy_expressive"),
        out_path=raw)
    print(f"audio: {vo.alignment.duration:.2f}s — {LINE!r}")

    # 2. Two anchor images (close + general), in parallel.
    async def _anchor(tag, prompt):
        imgs = await image_client.generate_async(
            prompt=prompt, tier="hero", source="workflow")
        print(f"[{tag}] anchor → {imgs[0].url}")
        return tag, imgs[0].url
    anchors = dict(await asyncio.gather(*[_anchor(t, p) for t, p in ANCHORS.items()]))

    # 3. Two Seedance clips.
    clips = {}
    for tag in ANCHORS:
        clips[tag] = await _seedance(video_client, anchors[tag], tag)

    # 4. HeyGen lipsync each (sequential — Replicate throttle).
    for tag in ANCHORS:
        await _heygen(lip, uploader, clips[tag], raw, tag)

    print("\n=== WATCH THESE ===")
    for tag in ANCHORS:
        print(f"  {tag:8} BEFORE: {OUT}/BEFORE_{tag}.mp4   AFTER: {OUT}/AFTER_{tag}.mp4")
    print("Compare the mouth in BEFORE vs AFTER against the spoken line.")


if __name__ == "__main__":
    asyncio.run(main())
