"""Verify the new ReplicateHeyGenLipsync provider end-to-end: take a clean face
clip, generate a fresh ISOLATED ElevenLabs line (a different line than the clip
'said'), lip-sync via heygen/lipsync-precision on Replicate, download + extract a
frame. Confirms (a) the Replicate HeyGen call works with our token, (b) the mouth
re-syncs to the supplied audio, (c) the face stays clean (no full re-render).
"""
import asyncio
import subprocess
from pathlib import Path

from app.config import load_settings
from app.lipsync import make_lipsync_client
from app.stitcher import download
from app.uploader import make_uploader
from app.voice import make_voice_client, voice_for

CLIP = Path("jobs/probe-native-verify/verify.mp4")   # clean face clip
OUT = Path("jobs/probe-replicate-heygen")
LINE = "Honestly, this is the one product I cannot stop recommending to everyone."


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    voice, uploader = make_voice_client(s), make_uploader(s)
    client = make_lipsync_client(s, "heygen_replicate")
    print(f"client: {type(client).__name__}  video_needs_audio={client.video_needs_audio}")

    # Isolated TTS — the full line, its own audio (polished Indian-English voice).
    vo = await voice.generate_vo(
        script=LINE, voice_id=voice_for("female", "warm_relatable"),
        out_path=OUT / "line.mp3")
    audio_dur = vo.alignment.duration
    print(f"isolated audio: {audio_dur:.2f}s")

    # heygen/lipsync-precision needs audio & video within 15% — pad the isolated
    # audio with trailing silence up to the clip's duration (mouth speaks, then
    # rests). In the real pipeline the clip is sized to the audio first.
    clip_dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "csv=p=0", str(CLIP)],
                capture_output=True, text=True).stdout.strip())
    padded = OUT / "line_padded.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(OUT / "line.mp3"),
                    "-af", f"apad=whole_dur={clip_dur:.2f}", "-t", f"{clip_dur:.2f}",
                    "-c:a", "pcm_s16le", "-ar", "44100", str(padded)],
                   check=True, capture_output=True)
    print(f"padded audio to clip dur {clip_dur:.2f}s")
    video_url = uploader.upload(str(CLIP))
    audio_url = uploader.upload(str(padded))
    print("running heygen/lipsync-precision on Replicate...")
    res = await client.lipsync(video_url=video_url, audio_url=audio_url,
                               output_file_name="replicate-heygen-probe")
    out = OUT / "synced.mp4"
    download(res.output_url, out)
    cdur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", str(out)],
                 capture_output=True, text=True).stdout.strip())
    subprocess.run(["ffmpeg", "-y", "-ss", "1.5", "-i", str(out), "-frames:v", "1",
                    "-q:v", "2", str(OUT / "synced_frame.jpg")], capture_output=True)
    print(f"\noutput: {out}  dur={cdur:.2f}s (audio {audio_dur:.2f}s)")
    print(f"frame: {OUT/'synced_frame.jpg'}  url: {res.output_url}")
    print("CHECK: mouth matches the new line; face clean; dur ~ audio length.")


if __name__ == "__main__":
    asyncio.run(main())
