"""Probe: does Seedance r2v REST the mouth during trailing reference silence,
or hallucinate filler lip-motion?

Generates ~3s of ElevenLabs speech, pads it with ~3s trailing silence (total
~6s reference), drives a real-human anchor via reference-to-video at duration=6,
and saves the clip so we can eyeball: speak ~3s then still/resting mouth ~3s
(GOOD → silence-padding fixes the 4s-floor padding) vs garbled/filler through
6s (BAD).
"""
import asyncio
import subprocess
from pathlib import Path

from app.config import NATIVE_LIPSYNC_MODEL, load_settings
from app.video_gen import VideoRequest, make_video_client
from app.uploader import make_uploader
from app.voice import make_voice_client

OUT = Path("jobs/probe-native-silence")
ANCHOR_URL = ("https://upload-data-shoppin-as1.s3.ap-south-1.amazonaws.com/"
              "product_flatlays/crops/"
              "c843e6be41888777ac4a82c0cb148d049b2a18324d91962f6758784620fd74d7.jpg")
LINE = "Honestly, this is the one thing I will never skip again."
SPEECH_TAIL_SILENCE_S = 3.0
CLIP_DURATION = 6


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    voice = make_voice_client(settings)
    uploader = make_uploader(settings)
    video = make_video_client(settings)

    # 1. ElevenLabs speech (~3s).
    vo = await voice.generate_vo(
        script=LINE, voice_id="Ih3XRGwQe2qczi6DzW48", out_path=OUT / "speech.mp3",
    )
    speech_s = vo.alignment.duration
    print(f"speech duration: {speech_s:.2f}s")

    # 2. Pad with trailing silence → reference length == clip length.
    ref = OUT / "ref_padded.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(OUT / "speech.mp3"),
         "-af", f"apad=pad_dur={SPEECH_TAIL_SILENCE_S}",
         "-t", str(CLIP_DURATION), "-c:a", "pcm_s16le", "-ar", "44100", str(ref)],
        check=True, capture_output=True,
    )
    ref_dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(ref)], check=True, capture_output=True, text=True,
    ).stdout.strip())
    print(f"padded reference duration: {ref_dur:.2f}s (speech {speech_s:.2f}s + "
          f"~{ref_dur - speech_s:.2f}s silence)")

    # 3. Drive r2v with the padded reference at the full clip duration.
    audio_url = uploader.upload(str(ref))
    req = VideoRequest(
        prompt=("Filmed on an iPhone front camera, handheld with a slight "
                "natural sway. The creator looks at the lens and speaks "
                "naturally, then stops and holds a calm, closed-mouth resting "
                "expression. Her voice and lip movements follow Audio 1."),
        first_frame_url=ANCHOR_URL, last_frame_url=None,
        duration=CLIP_DURATION, aspect_ratio="9:16", resolution="720p",
        generate_audio=True, seed=42, model=NATIVE_LIPSYNC_MODEL,
        camera_fixed=False, audio_url=audio_url,
    )
    task_id, _ = await video.create_task_async(req)
    print(f"r2v task: {task_id} — polling...")
    final = None
    async for status, resp in video.poll_task_stream_async(task_id):
        if resp is not None:
            final = resp
    if final is None:
        raise RuntimeError("no result")

    # 4. Download + probe.
    from app.stitcher import download
    clip = OUT / "probe_clip.mp4"
    download(final.video_url, clip)
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,duration:format=duration", "-of", "default=nw=1",
         str(clip)], capture_output=True, text=True,
    ).stdout
    print(f"\nSAVED → {clip}\n{info}")
    print(f"CHECK: mouth should speak ~{speech_s:.1f}s then REST for the "
          f"remaining ~{ref_dur - speech_s:.1f}s. If it keeps talking/garbles "
          f"through {CLIP_DURATION}s, silence-padding does NOT work.")


if __name__ == "__main__":
    asyncio.run(main())
