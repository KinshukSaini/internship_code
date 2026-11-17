"""Probe: does EvoLink Seedance r2v do TIMBRE reference (generate FRESH speech
in a reference voice) or only CONTENT follow (re-speak the reference audio's
words verbatim)?

Discriminating design:
  - Anchor audio (Audio 1) says LINE_X  ("...never skip again.")
  - Prompt tells the creator to say LINE_Y ("...morning routine...") and that
    the "voice timbre references Audio 1".
  - We generate, extract the clip's audio, and TRANSCRIBE it via ElevenLabs
    Scribe.

Verdict (objective, on words):
  transcript ~= LINE_Y  -> TIMBRE reference works (fresh speech). [what we want]
  transcript ~= LINE_X  -> CONTENT follow only (re-speaks the anchor).
  garbled/empty         -> inconclusive.

Test B (control): pure-text native audio on the i2v model (no audio ref) —
does EvoLink generate SPEECH of LINE_Y from a quoted line in the prompt at all?

Voice-timbre MATCH (does Test A sound like the anchor?) is subjective — the
clips are saved so you can listen. This script proves the content-vs-timbre
question, which is the one the whole design hinges on.
"""
import asyncio
import subprocess
from pathlib import Path

import httpx

from app.config import NATIVE_LIPSYNC_MODEL, load_settings
from app.stitcher import download
from app.uploader import make_uploader
from app.video_gen import DEFAULT_MODEL_ID, VideoRequest, make_video_client

OUT = Path("jobs/probe-timbre")
ANCHOR_IMG = Path("scratch-probe/anchor_face.jpg")          # real human face
ANCHOR_AUDIO = Path("jobs/probe-native-silence/speech.mp3")  # says LINE_X
LINE_X = "Honestly, this is the one thing I will never skip again."
LINE_Y = "Wait, you have to hear about my morning routine, it totally changed my skin."
DURATION = 8


def transcribe(api_key: str, media: Path) -> str:
    """ElevenLabs Scribe STT on the audio track of a media file."""
    wav = media.with_suffix(".stt.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
         str(wav)], check=True, capture_output=True,
    )
    with open(wav, "rb") as fh:
        resp = httpx.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": api_key},
            data={"model_id": "scribe_v1"},
            files={"file": (wav.name, fh, "audio/wav")},
            timeout=120,
        )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def overlap(a: str, b: str) -> float:
    """Crude word-overlap score (Jaccard) to compare a transcript to a line."""
    wa = {w.strip(".,!?—-").lower() for w in a.split()} - {""}
    wb = {w.strip(".,!?—-").lower() for w in b.split()} - {""}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


async def run_clip(video, req: VideoRequest, out: Path) -> Path:
    task_id, _ = await video.create_task_async(req)
    print(f"  task {task_id} — polling...")
    final = None
    async for _status, resp in video.poll_task_stream_async(task_id):
        if resp is not None:
            final = resp
    if final is None:
        raise RuntimeError("no result")
    download(final.video_url, out)
    return out


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    uploader = make_uploader(settings)
    video = make_video_client(settings)

    img_url = uploader.upload(str(ANCHOR_IMG))
    audio_url = uploader.upload(str(ANCHOR_AUDIO))
    print(f"anchor image: {img_url}\nanchor audio (LINE_X): {audio_url}\n")

    # --- Test A: timbre reference (r2v + audio ref + "timbre references Audio 1") ---
    print("TEST A — timbre reference (r2v, audio ref, prompt says LINE_Y):")
    req_a = VideoRequest(
        prompt=(
            "Filmed on an iPhone front camera, handheld with a slight natural "
            "sway, even soft daylight on the face. The creator looks at the lens "
            f'and says: "{LINE_Y}" Her voice timbre references Audio 1. She '
            "speaks the full line naturally at real-time pace."),
        first_frame_url=img_url, last_frame_url=None, duration=DURATION,
        aspect_ratio="9:16", resolution="720p", generate_audio=True, seed=42,
        model=NATIVE_LIPSYNC_MODEL, camera_fixed=False, audio_url=audio_url,
    )
    clip_a = await run_clip(video, req_a, OUT / "A_timbre.mp4")

    # --- Test B: pure-text native audio (i2v, no audio ref, quoted line) ---
    print("TEST B — pure-text native audio (i2v, no audio ref):")
    req_b = VideoRequest(
        prompt=(
            "Filmed on an iPhone front camera, handheld with a slight natural "
            "sway, even soft daylight on the face. A warm, friendly young woman "
            "with a polished, neutral Indian-English accent looks at the lens "
            f'and says: "{LINE_Y}" She speaks naturally at real-time pace.'),
        first_frame_url=img_url, last_frame_url=None, duration=DURATION,
        aspect_ratio="9:16", resolution="720p", generate_audio=True, seed=42,
        model=DEFAULT_MODEL_ID, camera_fixed=False, audio_url=None,
    )
    clip_b = await run_clip(video, req_b, OUT / "B_puretext.mp4")

    # --- Transcribe + verdict ---
    print("\n=== TRANSCRIPTS (ElevenLabs Scribe) ===")
    for name, clip in [("A_timbre", clip_a), ("B_puretext", clip_b)]:
        try:
            txt = transcribe(settings.elevenlabs_api_key, clip)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] transcription FAILED: {exc}")
            continue
        sx, sy = overlap(txt, LINE_X), overlap(txt, LINE_Y)
        verdict = ("TIMBRE (fresh speech of LINE_Y)" if sy > sx + 0.15
                   else "CONTENT-FOLLOW (re-speaks LINE_X)" if sx > sy + 0.15
                   else "INCONCLUSIVE")
        print(f"\n[{name}] {clip}")
        print(f"  transcript: {txt!r}")
        print(f"  overlap  LINE_X={sx:.2f}  LINE_Y={sy:.2f}  -> {verdict}")

    print(f"\nSAVED clips in {OUT}/ — LISTEN to A_timbre.mp4 and compare its "
          f"VOICE to {ANCHOR_AUDIO} to judge timbre MATCH (subjective).")


if __name__ == "__main__":
    asyncio.run(main())
