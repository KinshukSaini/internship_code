"""End-to-end-ish verification (no Claude needed): generate ONE native r2v clip
from the NEW flattering char-ref using the NEW _native_talking_prompt (brisk pace
+ flattering skin) at a tight duration, then save + extract a frame so we can
confirm (a) the face stays CLEAN through the r2v re-render and (b) speech is
natural-paced (speech_end ~ natural line length, not stretched).
"""
import asyncio
import subprocess
import types
from pathlib import Path

from app.assembly import _detect_speech_end
from app.config import NATIVE_LIPSYNC_MODEL, load_settings
from app.gemini import VoicePersona, render_voice_persona
from app.pipeline import _native_talking_prompt
from app.stitcher import download
from app.uploader import make_uploader
from app.video_gen import VideoRequest, make_video_client
from app.voice import make_voice_client, script_fidelity

FACE = Path("jobs/probe-face/char_ref_720.jpg")          # NEW clean still
ANCHOR = Path("jobs/workflows/v2-20260630-121447/timbre-anchor.wav")
OUT = Path("jobs/probe-native-verify")
LINE = "Men, if you're trying to impress a woman, here's a small tip."  # 13w ~4.3s
DUR = 5   # round(4.3)=4 risks truncation; 5 with the brisk+rest prompt should rest


def pad_ref(dst: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-i", str(ANCHOR),
                    "-af", f"apad=whole_dur={DUR}", "-t", str(DUR),
                    "-c:a", "pcm_s16le", "-ar", "44100", str(dst)],
                   check=True, capture_output=True)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    video, uploader, voice = make_video_client(s), make_uploader(s), make_voice_client(s)
    persona = render_voice_persona(VoicePersona(
        gender="female", age_band="late-20s", timbre="warm clear",
        pace="brisk", warmth="confident"))
    beat = types.SimpleNamespace(ad_section="hook", dialogue_text=LINE)
    prompt = _native_talking_prompt(beat, persona)
    print("=== PROMPT (the real pipeline native prompt) ===\n" + prompt + "\n")

    ref = OUT / "ref.wav"; pad_ref(ref)
    img_url, audio_url = uploader.upload(str(FACE)), uploader.upload(str(ref))
    req = VideoRequest(
        prompt=prompt, first_frame_url=img_url, last_frame_url=None,
        duration=DUR, aspect_ratio="9:16", resolution="720p",
        generate_audio=True, seed=42, model=NATIVE_LIPSYNC_MODEL,
        camera_fixed=False, audio_url=audio_url,
    )
    task_id, _ = await video.create_task_async(req)
    print(f"r2v task {task_id} — polling...")
    final = None
    async for _st, resp in video.poll_task_stream_async(task_id):
        if resp is not None:
            final = resp
    clip = OUT / "verify.mp4"
    download(final.video_url, clip)
    subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(clip), "-frames:v", "1",
                    "-q:v", "2", str(OUT / "verify_frame.jpg")],
                   capture_output=True)
    cdur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", str(clip)],
                 capture_output=True, text=True).stdout.strip())
    spe = _detect_speech_end(clip)
    print(f"\nclip_dur={cdur:.2f}s  speech_end={spe}  (line ~4.3s natural)")
    print(f"SAVED clip {clip} + frame {OUT/'verify_frame.jpg'}")
    print("CHECK: speech_end ~4-4.5s (natural, not stretched to 5) + face clean.")


if __name__ == "__main__":
    asyncio.run(main())
