"""Probe: does native r2v stretch speech to fill the ORDERED duration, and does
sizing duration to the line + a length-matched (silence-padded) reference fix
the slow delivery — while keeping the good native lip-sync?

Hypothesis (from the run audit + the audio research workflow): r2v fills the
requested clip duration with speech, so over-ordering (ceil + 4s floor) stretches
the delivery. Fix = order ~= natural line length and pad the reference to the
ordered duration so the model RESTS the mouth instead of stretching.

Arms (same line/anchor/seed/prompt; vary only duration + reference length):
  A  L0 dur=7  ref=anchor padded to 7   -> reproduce severe stretch
  B  L0 dur=5  ref=anchor padded to 5   -> shipped behavior (ceil 4.33->5)
  C  L0 dur=4  ref=anchor padded to 4   -> round-DOWN: truncates the 4.33s line, or fast?
  D  L9 dur=4  ref=anchor as-is (7s)    -> the bug on a sub-4s line (floor stretch)
  E  L9 dur=4  ref=anchor padded to 4   -> padded ref: speak ~2.4s then REST?
  F  L0 dur=5  ref=anchor SLICED to 3s, padded to 5  -> Reddit short-anchor tip

For each: ffprobe duration, _detect_speech_end (where speech stops), ElevenLabs
Scribe transcript + word count -> words/sec, fidelity. Clips saved to WATCH for
lip-sync + pace judgment.
"""
import asyncio
import subprocess
from pathlib import Path

from app.assembly import _detect_speech_end
from app.config import NATIVE_LIPSYNC_MODEL, load_settings
from app.stitcher import download
from app.uploader import make_uploader
from app.video_gen import VideoRequest, make_video_client
from app.voice import make_voice_client, script_fidelity

RUN = Path("jobs/workflows/v2-20260630-121447")
OUT = Path("jobs/probe-native-pace")
FACE = Path("scratch-probe/pace_face.jpg")
ANCHOR = RUN / "timbre-anchor.wav"   # 7s slice of the ElevenLabs VO

L0 = "Men, if you're trying to impress a woman, here's a small tip."   # 13 words ~4.3s natural
L9 = "Wear it, and heads will turn."                                   # 7 words ~2.4s natural

ARMS = [
    ("A_L0_dur7_pad7", L0, 7, 7, 7),
    ("B_L0_dur5_pad5", L0, 5, 7, 5),
    ("C_L0_dur4_pad4", L0, 4, 7, 4),
    ("D_L9_dur4_raw7", L9, 4, 7, None),   # None = use anchor as-is, no pad
    ("E_L9_dur4_pad4", L9, 4, 7, 4),
    ("F_L0_dur5_short3pad5", L0, 5, 3, 5),
]


def make_ref(slice_s: int, pad_to, dst: Path) -> None:
    """Slice the anchor to slice_s seconds; if pad_to set, pad trailing silence
    to pad_to seconds (reference length == ordered clip duration)."""
    sliced = dst.with_suffix(".sliced.wav")
    subprocess.run(["ffmpeg", "-y", "-i", str(ANCHOR), "-t", str(slice_s),
                    "-c:a", "pcm_s16le", "-ar", "44100", str(sliced)],
                   check=True, capture_output=True)
    if pad_to is None:
        sliced.rename(dst)
        return
    subprocess.run(["ffmpeg", "-y", "-i", str(sliced),
                    "-af", f"apad=whole_dur={pad_to}", "-t", str(pad_to),
                    "-c:a", "pcm_s16le", "-ar", "44100", str(dst)],
                   check=True, capture_output=True)


def prompt_for(line: str) -> str:
    return (
        "Filmed on an iPhone front camera, handheld with a slight natural sway, "
        "even soft daylight on the face. The creator looks at the lens and says "
        f'exactly: "{line}" — speaking the full line naturally at a brisk, '
        "real-time pace, then holds a calm closed-mouth expression. Her voice "
        "timbre references Audio 1. She says exactly the quoted line and nothing "
        "else — no added, improvised, or repeated words."
    )


async def run_arm(video, uploader, voice, img_url, name, line, dur, slice_s, pad_to):
    ref = OUT / f"{name}-ref.wav"
    make_ref(slice_s, pad_to, ref)
    audio_url = uploader.upload(str(ref))
    req = VideoRequest(
        prompt=prompt_for(line), first_frame_url=img_url, last_frame_url=None,
        duration=dur, aspect_ratio="9:16", resolution="720p",
        generate_audio=True, seed=42, model=NATIVE_LIPSYNC_MODEL,
        camera_fixed=False, audio_url=audio_url,
    )
    task_id, _ = await video.create_task_async(req)
    final = None
    async for _s, resp in video.poll_task_stream_async(task_id):
        if resp is not None:
            final = resp
    if final is None:
        return (name, "NO RESULT", None)
    clip = OUT / f"{name}.mp4"
    download(final.video_url, clip)
    cdur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(clip)], capture_output=True, text=True).stdout.strip())
    try:
        speech_end = _detect_speech_end(clip)
    except Exception:
        speech_end = None
    try:
        tx = await voice.transcribe(media_path=clip)
    except Exception as e:  # noqa: BLE001
        tx = f"<stt failed: {e}>"
    words = len([w for w in tx.replace("(", " ").replace(")", " ").split() if w.isalpha() or w.isalnum()])
    sp = speech_end if speech_end else cdur
    wps = words / sp if sp else 0
    fid = script_fidelity(tx, line)
    return (name, {
        "ordered": dur, "clip_dur": round(cdur, 2),
        "speech_end": round(speech_end, 2) if speech_end else None,
        "words": words, "wps": round(wps, 2), "fidelity": round(fid, 2),
        "transcript": tx,
    }, clip)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    video = make_video_client(s)
    uploader = make_uploader(s)
    voice = make_voice_client(s)
    img_url = uploader.upload(str(FACE))
    print(f"face: {img_url}\nanchor: {ANCHOR} (7s)\n")
    results = await asyncio.gather(*[
        run_arm(video, uploader, voice, img_url, *arm) for arm in ARMS
    ], return_exceptions=True)
    print("\n=== PACE PROBE RESULTS ===")
    print(f"{'arm':<22} {'ord':>3} {'clip':>5} {'spch':>5} {'wrds':>4} {'wps':>5} {'fid':>4}")
    for r in results:
        if isinstance(r, BaseException):
            print(f"  ERROR: {r!r}"); continue
        name, d, _clip = r
        if not isinstance(d, dict):
            print(f"{name:<22} {d}"); continue
        print(f"{name:<22} {d['ordered']:>3} {d['clip_dur']:>5} "
              f"{str(d['speech_end']):>5} {d['words']:>4} {d['wps']:>5} {d['fidelity']:>4}")
    print("\nTranscripts:")
    for r in results:
        if isinstance(r, tuple) and isinstance(r[1], dict):
            print(f"  {r[0]}: {r[1]['transcript']!r}")
    print(f"\nWATCH clips in {OUT}/ — compare A(slow) vs B/C(tighter) pace + lip-sync; "
          f"D(stretch) vs E(rest) on the short line; F = short 3s anchor.")
    print("INTERPRET: if speech_end tracks 'ordered' (A~7,B~5,C~4) → duration drives "
          "pace → fix = order tight. If E rests (speech_end ~2.4 < 4) while D stretches "
          "(~4) → padding the reference makes r2v rest. Higher wps = faster (less slow).")


if __name__ == "__main__":
    asyncio.run(main())
