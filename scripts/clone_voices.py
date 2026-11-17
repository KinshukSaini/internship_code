"""Characterize + clone a batch of audio samples, upload them to S3, log to JSON.

Input is one or more ZIP archives of .mp3 samples (the Instagram / YouTube
exports). Pipeline, per sample, run concurrently and throttled by per-API
semaphores:

  1. Extract the sample from the zip (staged in scratchpad).
  2. Characterize the VOICE with Gemini (audio in → gender + language + a full
     tag set + a casting summary). Gender and language are DETECTED from the
     audio, not supplied. A compact trimmed clip is sent so the big files stay
     under the inline-request limit.
  3. Upload the ORIGINAL sample to S3 (content-addressable) — URL is stored.
  4. Clone the voice with ElevenLabs Instant Voice Cloning → voice_id
     (remove_background_noise=True, since these exports carry background music).

The audio is NOT kept locally — only the S3 link, the Gemini characteristics,
and the ElevenLabs clone id land in the output JSON. The JSON is written
incrementally and the run is RESUMABLE: a re-run loads the existing file and
skips any sample already "done", so hitting the ElevenLabs custom-voice cap
mid-run never re-burns a slot on re-run.

    uv run python scripts/clone_voices.py --limit 3     # validation pass
    uv run python scripts/clone_voices.py               # full run (resumes)

Env (from .env): GEMINI_API_KEY, ELEVENLABS_API_KEY, AWS_* / S3_* (see config).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field

from app.config import load_settings
from app.uploader import make_uploader
from app.voice import VoiceVibe

# ── Defaults ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS = Path("/Users/harshkedia/Downloads")
DEFAULT_ZIPS = [
    (DOWNLOADS / "All Insta 2026-07-22.zip", "insta"),
    (DOWNLOADS / "All YT July 22 2026.zip", "yt"),
]
OUT_JSON = ROOT / "data" / "voice_clones" / "voices.json"
STAGE_DIR = Path(tempfile.gettempdir()) / "clone_voices_stage"
S3_KEY_PREFIX = "voice_samples"
CLONE_NAME_PREFIX = "OCLabs"

# Gemini model for voice characterization (audio understanding). 3.6-flash is
# the current flash tier (verified live); 3.5-flash is the fallback. Kept local
# to this one-off script so it doesn't shift the pipeline's global model config.
VOICE_MODEL = "gemini-3.6-flash"
VOICE_MODEL_FALLBACK = "gemini-3.5-flash"

# Seconds of audio sent to Gemini for characterization. A short mono clip is
# plenty to judge a voice and keeps even the 90MB YT files under the inline
# request limit.
CHARACTERIZE_CLIP_S = 90

# Per-API concurrency (Gemini is the tight one — user flagged rate limits).
GEMINI_CONCURRENCY = 5
ELEVENLABS_CONCURRENCY = 3
PREP_CONCURRENCY = 6            # ffmpeg trims


# ── Gemini characteristics schema ─────────────────────────────────────────────

class VoiceCharacteristics(BaseModel):
    gender: str = Field(description="male / female / ambiguous — as HEARD in the speech.")
    language: str = Field(
        description="Primary spoken language of the speech. Use 'Hinglish' for "
                    "English-Hindi code-mixing; otherwise e.g. 'English', "
                    "'Hindi'. Note a second language in parentheses if clearly "
                    "code-mixed, e.g. 'Hinglish (English-dominant)'.")
    age_band: str = Field(description="e.g. 'late-teens', 'early-20s', 'mid-30s', '40s+'.")
    accent_region: str = Field(
        description="Accent + likely region, e.g. 'neutral urban Indian-English', "
                    "'North-Indian Hindi', 'South-Indian English'.")
    timbre: str = Field(description="2-4 words: e.g. 'warm bright', 'soft husky', 'clear crisp'.")
    pitch: str = Field(description="Perceived pitch: e.g. 'low', 'medium', 'high'.")
    pace: str = Field(description="Delivery pace: e.g. 'calm conversational', 'punchy upbeat'.")
    tone_warmth: str = Field(description="e.g. 'friendly', 'confident', 'soothing', 'authoritative'.")
    energy: str = Field(description="Energy level: e.g. 'low-key', 'moderate', 'high-energy'.")
    voice_vibe: VoiceVibe = Field(
        description="Best-fit vibe from the fixed catalog: upbeat_lively, "
                    "warm_relatable, sassy_expressive, youthful_excited, "
                    "professional_clear, soothing_narrative, crisp_informative, "
                    "mature_authentic.")
    has_background_music: bool = Field(
        description="True if background music/beat is audible under the speech "
                    "(a clone-quality signal for these social exports).")
    summary: str = Field(description="1-2 sentence natural-language description of the voice.")


_CHARACTERIZE_PROMPT = (
    "You are a professional voice-casting director analyzing the audio of a "
    "short-form social video. There may be background music under the speech — "
    "focus ONLY on the human speaking voice. From the speech, DETERMINE the "
    "speaker's gender, the spoken language (mark 'Hinglish' for English-Hindi "
    "code-mixing), age band, accent/region, timbre, pitch, pace, tone/warmth, "
    "and energy; flag whether background music is present; pick the single "
    "best-fit voice_vibe from the catalog; and write a 1-2 sentence casting "
    "summary. Judge everything from what you actually hear."
)


# ── Audio prep (ffmpeg trim for characterization) ─────────────────────────────

def _trim_clip(src: Path, dst: Path) -> None:
    """Compact mono clip for Gemini: first CHARACTERIZE_CLIP_S seconds @ 48kbps."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-t", str(CHARACTERIZE_CLIP_S),
         "-ac", "1", "-b:a", "48k", str(dst)],
        check=True, capture_output=True,
    )


# ── Gemini characterize ───────────────────────────────────────────────────────

async def _characterize(genai_client, sem: asyncio.Semaphore, *,
                        clip: bytes) -> VoiceCharacteristics:
    from google.genai import types

    contents = [types.Part.from_bytes(data=clip, mime_type="audio/mp3"),
                _CHARACTERIZE_PROMPT]
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=VoiceCharacteristics,
        temperature=0.3,
    )
    async with sem:
        last_exc: Exception | None = None
        for model in (VOICE_MODEL, VOICE_MODEL_FALLBACK):
            try:
                resp = await genai_client.aio.models.generate_content(
                    model=model, contents=contents, config=config)
                return VoiceCharacteristics.model_validate_json(resp.text)
            except Exception as e:  # noqa: BLE001 — fall through to fallback model
                last_exc = e
        raise RuntimeError(f"Gemini characterization failed: {last_exc}")


# ── ElevenLabs clone ──────────────────────────────────────────────────────────

async def _clone(el_client, sem: asyncio.Semaphore, *, name: str,
                 audio: bytes, fname: str, labels: dict[str, str],
                 summary: str) -> str:
    """Return voice_id. Raises on failure (e.g. voice-limit reached)."""
    clean = {k: v for k, v in labels.items() if v}
    async with sem:
        resp = await el_client.voices.ivc.create(
            name=name,
            files=[(fname, audio, "audio/mpeg")],
            remove_background_noise=True,   # exports carry background music
            description=(summary[:500] or None),
            # Multipart form field → must be a JSON string, not a raw dict.
            labels=json.dumps(clean),
        )
    vid = getattr(resp, "voice_id", None) or (
        resp.get("voice_id") if isinstance(resp, dict) else None)
    if not vid:
        raise RuntimeError(f"ElevenLabs IVC returned no voice_id: {resp!r}")
    return vid


# ── Output store (incremental, atomic, resumable) ─────────────────────────────

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.records: dict[str, dict] = {}
        if path.exists():
            for rec in json.loads(path.read_text() or "[]"):
                self.records[rec["key"]] = rec

    def is_done(self, key: str) -> bool:
        return self.records.get(key, {}).get("status") == "done"

    async def put(self, rec: dict) -> None:
        async with self._lock:
            self.records[rec["key"]] = rec
            ordered = sorted(self.records.values(), key=lambda r: r.get("row", 0))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(ordered, indent=2, ensure_ascii=False))
            tmp.replace(self.path)


# ── Sample discovery ──────────────────────────────────────────────────────────

class Sample:
    def __init__(self, key: str, platform: str, name: str, path: Path, row: int):
        self.key = key            # stable resume key (platform/archive-name)
        self.platform = platform
        self.source_name = name   # original filename inside the zip
        self.path = path          # staged local path
        self.row = row

    @property
    def clone_name(self) -> str:
        m = re.match(r"\s*(\d+)", Path(self.source_name).stem)
        slug = m.group(1) if m else str(self.row)
        return f"{CLONE_NAME_PREFIX}-{self.platform}-{slug}"


def _discover(zips: list[tuple[Path, str]], stage: Path) -> list[Sample]:
    """Extract each zip's .mp3 entries into `stage` and return ordered Samples."""
    stage.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []
    row = 0
    for zpath, platform in zips:
        if not zpath.exists():
            print(f"WARNING: zip not found, skipping: {zpath}", file=sys.stderr)
            continue
        with zipfile.ZipFile(zpath) as zf:
            names = sorted(n for n in zf.namelist()
                           if n.lower().endswith(".mp3") and not n.endswith("/"))
            for n in names:
                row += 1
                base = Path(n).name
                out = stage / platform / base
                out.parent.mkdir(parents=True, exist_ok=True)
                if not out.exists() or out.stat().st_size == 0:
                    with zf.open(n) as src, open(out, "wb") as dst:
                        dst.write(src.read())
                samples.append(Sample(f"{platform}/{base}", platform, base, out, row))
    return samples


# ── Per-sample orchestration ──────────────────────────────────────────────────

async def _process(sample: Sample, *, genai_client, el_client, uploader,
                   store: Store, sems: dict[str, asyncio.Semaphore]) -> None:
    if store.is_done(sample.key):
        print(f"[{sample.row:02d}] skip (done) {sample.key}")
        return

    base: dict[str, Any] = {
        "key": sample.key, "row": sample.row, "platform": sample.platform,
        "source_file": sample.source_name, "s3_url": None,
        "elevenlabs_voice_id": None, "elevenlabs_name": None,
        "characteristics": None, "status": "error", "error": None,
    }
    clip_path: Path | None = None
    try:
        audio = sample.path.read_bytes()

        # Trim a compact clip for Gemini (keeps big files under inline limit).
        fd, clip_name = tempfile.mkstemp(suffix=".mp3")
        clip_path = Path(clip_name)
        import os as _os
        _os.close(fd)
        async with sems["prep"]:
            await asyncio.to_thread(_trim_clip, sample.path, clip_path)
        clip = clip_path.read_bytes()

        # Characterize (Gemini) ∥ upload (S3) concurrently.
        async def _upload() -> str:
            return await asyncio.to_thread(
                uploader.upload, str(sample.path), key_prefix=S3_KEY_PREFIX)

        char_task = asyncio.create_task(
            _characterize(genai_client, sems["gemini"], clip=clip))
        s3_task = asyncio.create_task(_upload())

        chars = await char_task
        base["characteristics"] = chars.model_dump()
        base["s3_url"] = await s3_task

        vid = await _clone(
            el_client, sems["elevenlabs"], name=sample.clone_name,
            audio=audio, fname=sample.source_name, summary=chars.summary,
            labels={"language": chars.language, "gender": chars.gender,
                    "accent": chars.accent_region, "age": chars.age_band})
        base["elevenlabs_voice_id"] = vid
        base["elevenlabs_name"] = sample.clone_name
        base["status"] = "done"
        print(f"[{sample.row:02d}] done  {chars.gender:<6} {chars.language:<20} "
              f"vibe={chars.voice_vibe:<18} voice_id={vid}")
    except Exception as e:  # noqa: BLE001 — record + continue; run stays resumable
        base["error"] = f"{type(e).__name__}: {e}"
        print(f"[{sample.row:02d}] ERROR {sample.key}: {base['error'][:90]}")
    finally:
        if clip_path is not None:
            clip_path.unlink(missing_ok=True)
        await store.put(base)


async def _run(zips: list[tuple[Path, str]], limit: int | None) -> int:
    settings = load_settings()
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY is not set.", file=sys.stderr)
        return 1
    if not settings.elevenlabs_api_key:
        print("ELEVENLABS_API_KEY is not set.", file=sys.stderr)
        return 1

    samples = _discover(zips, STAGE_DIR)
    if limit is not None:
        samples = samples[:limit]

    from google import genai
    from elevenlabs.client import AsyncElevenLabs

    genai_client = genai.Client(api_key=settings.gemini_api_key)
    el_client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
    uploader = make_uploader(settings)
    store = Store(OUT_JSON)

    sems = {
        "prep": asyncio.Semaphore(PREP_CONCURRENCY),
        "gemini": asyncio.Semaphore(GEMINI_CONCURRENCY),
        "elevenlabs": asyncio.Semaphore(ELEVENLABS_CONCURRENCY),
    }

    todo = sum(1 for s in samples if not store.is_done(s.key))
    print(f"{len(samples)} samples ({todo} to process, {len(samples) - todo} "
          f"already done). Output → {OUT_JSON}")

    await asyncio.gather(*[
        _process(s, genai_client=genai_client, el_client=el_client,
                 uploader=uploader, store=store, sems=sems)
        for s in samples
    ])

    done = sum(1 for r in store.records.values() if r.get("status") == "done")
    err = sum(1 for r in store.records.values() if r.get("status") == "error")
    print(f"\nComplete. done={done}, error={err}, total_recorded={len(store.records)}.")
    if err:
        for r in sorted(store.records.values(), key=lambda x: x.get("row", 0)):
            if r.get("status") == "error":
                print(f"  - row {r.get('row')} {r['key']}: {r.get('error')}",
                      file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", action="append", default=None,
                    help="platform=path/to.zip (repeatable). Defaults to the "
                         "Insta + YT downloads.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N samples (validation pass).")
    args = ap.parse_args()

    if args.zip:
        zips = []
        for spec in args.zip:
            platform, _, path = spec.partition("=")
            zips.append((Path(path), platform))
    else:
        zips = DEFAULT_ZIPS
    return asyncio.run(_run(zips, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
