"""RCA: is HeyGen (Replicate) actually re-animating the mouth to our audio, or
passing Seedance's mouth through? Take one existing Seedance clip + a fresh,
DISTINCT audio line, and lipsync it two ways to isolate the cause:

  V1 mux_target  — mux the TARGET audio into the video (current pipeline)
  V2 mux_silence — mux a SILENT track in (validation only), target as `audio`

Emit before/after clips + SSIM(source, output) so we can see which (if either)
actually drives the mouth. High SSIM ≈ passthrough (HeyGen didn't change it).

Usage: uv run python scripts/rca_lipsync.py [clip.mp4]
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.assembly import attach_audio, pad_audio_to
from app.config import load_settings
from app.lipsync import make_lipsync_client
from app.stitcher import download
from app.uploader import make_uploader
from app.voice import make_voice_client, voice_for

# TARGET: short (fits a ~5s clip), lots of open/closed mouth variety so real
# sync is visually obvious.
LINE = "Wow, oh my, absolutely mind blowing!"
# A DIFFERENT line to embed as the video's "current speech" (V3), so HeyGen
# detects a speaker AND sees a gap to re-dub — mirrors what the probe's clip had.
EMBED_LINE = "The quiet morning fog settled over the sleepy little village."

OUT = Path("jobs/lipsync-rca")


def _probe_dur(p) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(p)],
        capture_output=True, text=True).stdout.strip())


def _ssim(a, b) -> str:
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b),
         "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"],
        capture_output=True, text=True)
    for ln in r.stderr.splitlines():
        if "SSIM" in ln:
            return ln.split("]", 1)[-1].strip()
    return "n/a"


def _frame(src, t, dst):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-ss", str(t), "-i", str(src), "-frames:v", "1",
                    str(dst), "-y"], check=True)


async def _lipsync(client, uploader, video: Path, audio: Path, tag: str) -> Path:
    vurl = uploader.upload(str(video))
    aurl = uploader.upload(str(audio))
    res = await client.lipsync(video_url=vurl, audio_url=aurl,
                               output_file_name=tag)
    out = OUT / f"synced_{tag}.mp4"
    download(res.output_url, out)
    return out


async def main(clip: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    voice = make_voice_client(s)
    uploader = make_uploader(s)
    client = make_lipsync_client(s, "heygen_replicate")

    clip_dur = _probe_dur(clip)
    print(f"source clip: {clip}  dur={clip_dur:.2f}s (silent Seedance)")

    # 1. Fresh distinct audio, padded to the clip duration (pipeline does this).
    raw = OUT / "target-raw.mp3"
    vo = await voice.generate_vo(
        script=LINE, voice_id=voice_for("female", "sassy_expressive"),
        out_path=raw)
    print(f"target audio: {vo.alignment.duration:.2f}s of speech")
    target = OUT / "target-padded.wav"
    pad_audio_to(raw, clip_dur, target)

    # 2. before.mp4 = source clip + target audio, NO lipsync (baseline mismatch).
    before = OUT / "before.mp4"
    attach_audio(clip, target, before, clip_dur)
    print(f"wrote {before} (source mouth + target audio, no lipsync)")

    # 3. V1: mux TARGET audio into the video (current pipeline behaviour).
    v1_in = OUT / "input_v1_mux_target.mp4"
    attach_audio(clip, target, v1_in, clip_dur)
    v1 = await _lipsync(client, uploader, v1_in, target, "v1_mux_target")
    print(f"V1 (mux target)  → {v1}   SSIM(src,v1)={_ssim(clip, v1)}")

    # 4. V3: mux a DIFFERENT speech line as the video's embedded audio (so a
    # speaker is detected AND there's a gap to re-dub), target as `audio`.
    embed_raw = OUT / "embed-raw.mp3"
    await voice.generate_vo(
        script=EMBED_LINE, voice_id=voice_for("female", "warm_relatable"),
        out_path=embed_raw)
    embed = OUT / "embed-padded.wav"
    pad_audio_to(embed_raw, clip_dur, embed)
    v3_in = OUT / "input_v3_mux_different.mp4"
    attach_audio(clip, embed, v3_in, clip_dur)
    v3 = await _lipsync(client, uploader, v3_in, target, "v3_mux_different")
    print(f"V3 (mux DIFFERENT speech) → {v3}   SSIM(src,v3)={_ssim(clip, v3)}")

    # 5. Comparison frames at a mid-speech timestamp.
    t = min(1.6, clip_dur / 2)
    _frame(clip, t, OUT / "frame_source.png")
    _frame(v1, t, OUT / "frame_v1.png")
    _frame(v3, t, OUT / "frame_v3.png")
    print(f"\nframes at t={t:.1f}s: {OUT}/frame_source.png  frame_v1.png  frame_v3.png")
    print("Interpretation: SSIM near 1.0 = HeyGen barely changed the video "
          "(passthrough). Lower SSIM on the variant that truly syncs.")


if __name__ == "__main__":
    clip = Path(sys.argv[1]) if len(sys.argv) > 1 \
        else Path("jobs/workflows/v2-20260702-160720/clip-0.mp4")
    asyncio.run(main(clip))
