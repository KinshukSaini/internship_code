"""Dedup the characterized samples into a distinct, female-leaning selection.

Reads data/voice_clones/voices.json (produced by clone_voices.py — every
sample already has Gemini characteristics + an S3 url + a staged local clip),
and greedily builds a set of MUTUALLY-DISTINCT voices to clone, capped at the
free ElevenLabs clone slots and biased slightly toward female voices.

Method (per gender bucket, buckets run concurrently):
  - Order samples by quality (clean audio first, then longer duration).
  - Walk the ordered list; for each candidate, ask Gemini whether it is the
    SAME SPEAKER as any already-SELECTED voice of that gender (same-gender
    comparisons only → bounded payload, higher precision). If it matches one,
    it's a duplicate and is skipped; otherwise it's selected.
  - Stop once the gender's target is reached.
Shortfalls in one gender are back-filled from the other so the total still
approaches the slot budget.

Writes the decision back into voices.json (adds `selected`, `duplicate_of`,
`dedup_confidence`) and prints a summary. Cloning is a separate step.

    uv run python scripts/dedup_select.py
    uv run python scripts/dedup_select.py --female 18 --male 12 --budget 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field

from app.config import load_settings

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "data" / "voice_clones" / "voices.json"
STAGE_DIR = Path(tempfile.gettempdir()) / "clone_voices_stage"
# Voice-ISOLATED comparison clips. Every source shares the same background
# music bed, which makes music-contaminated clips read as "same speaker" and
# over-merges distinct young voices — so we strip the music (ElevenLabs audio
# isolation) before any speaker comparison.
CLIP_DIR = STAGE_DIR / "dedup_iso_clips"

VOICE_MODEL = "gemini-3.6-flash"
VOICE_MODEL_FALLBACK = "gemini-3.5-flash"

# Short comparison clip: skip a few seconds of intro, take a ~12s mono window.
CLIP_SS, CLIP_T = 3, 12
# Accept a duplicate only at/above this confidence (avoids discarding distinct
# voices on a weak guess; a missed dup only costs one slot).
DUP_CONFIDENCE = 0.7
BUCKET_CONCURRENCY = 2   # female + male buckets in parallel
ISOLATE_CONCURRENCY = 3  # ElevenLabs audio-isolation fan-out
GEMINI_CONCURRENCY = 4   # global cap on speaker-match calls


class PairMatch(BaseModel):
    same_speaker: bool = Field(
        description="True ONLY if the SAME individual person is speaking in "
                    "both clips.")
    confidence: float = Field(description="0.0-1.0 confidence in the decision.")


# Pairwise (1-vs-1) is far more reliable than showing many similar references
# in one call — a single call with many young-female refs biases the model to
# 'matches one of them' and over-merges distinct voices.
_MATCH_PROMPT = (
    "You are a speaker-identification expert. Clip A and Clip B have had their "
    "background music removed. Judge ONLY vocal identity (timbre, pitch, "
    "resonance, articulation, breathiness) — ignore the words and delivery. Be "
    "STRICT: different young Indian speakers often share a similar age, accent "
    "and bright tone yet are DIFFERENT people — set same_speaker=true ONLY if "
    "it is clearly the SAME individual's voice in both clips."
)


def _clip_path(key: str) -> Path:
    return CLIP_DIR / (key.replace("/", "__") + ".mp3")


def _raw_clip(src: Path) -> bytes:
    """A ~12s mono window (skipping intro) as mp3 bytes — isolation input."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(CLIP_SS), "-t", str(CLIP_T), "-i", str(src),
         "-ac", "1", "-b:a", "48k", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True)
    return r.stdout


def _compact(raw: bytes, dst: Path) -> None:
    """Re-encode arbitrary audio bytes → small 32k mono mp3 (keeps Gemini
    comparison payloads light even with many reference clips per call)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", "pipe:0", "-ac", "1", "-b:a", "32k", str(dst)],
        input=raw, check=True, capture_output=True)


async def _isolate_clip(el_client, sem, src: Path, dst: Path) -> str | None:
    """Isolate speech from a source file's clip and cache a compact mp3 at dst.
    Returns an error string on failure, else None."""
    if dst.exists() and dst.stat().st_size > 0:
        return None
    if not src.exists():
        return f"missing staged file {src}"
    try:
        raw = await asyncio.to_thread(_raw_clip, src)
        async with sem:
            chunks: list[bytes] = []
            async for c in el_client.audio_isolation.convert(
                    audio=("clip.mp3", raw, "audio/mpeg")):
                chunks.append(c)
        iso = b"".join(chunks)
        if not iso:
            return "empty isolation output"
        await asyncio.to_thread(_compact, iso, dst)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


async def _pair_match(genai_client, sem, *, a: bytes, b: bytes) -> PairMatch:
    """1-vs-1 same-speaker judgment on two isolated clips (with retry)."""
    from google.genai import types

    contents = ["Clip A:", types.Part.from_bytes(data=a, mime_type="audio/mp3"),
                "Clip B:", types.Part.from_bytes(data=b, mime_type="audio/mp3"),
                _MATCH_PROMPT]
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=PairMatch, temperature=0.1)
    async with sem:
        last: Exception | None = None
        # Retry across models with exponential backoff — gemini-3.6-flash
        # returns transient 503 "high demand" / 429 under load.
        for attempt in range(5):
            for model in (VOICE_MODEL, VOICE_MODEL_FALLBACK):
                try:
                    resp = await genai_client.aio.models.generate_content(
                        model=model, contents=contents, config=config)
                    return PairMatch.model_validate_json(resp.text)
                except Exception as e:  # noqa: BLE001
                    last = e
            await asyncio.sleep(2.0 * (2 ** attempt))   # 2,4,8,16,32s
        raise RuntimeError(f"speaker-match failed after retries: {last}")


def _quality_key(rec: dict) -> tuple:
    """Best samples first: clean audio (no music), then larger file (longer)."""
    ch = rec.get("characteristics") or {}
    has_music = bool(ch.get("has_background_music"))
    size = _clip_path(rec["key"]).exists() and _clip_path(rec["key"]).stat().st_size or 0
    return (has_music, -size)


async def _select_bucket(genai_client, sem, *, recs: list[dict], target: int,
                         label: str) -> tuple[list[dict], list[dict]]:
    """Greedy distinct-voice selection within one gender bucket.

    Each candidate is compared 1-vs-1 against every already-selected rep (in
    parallel); it's a duplicate iff any comparison says same_speaker at/above
    the confidence threshold, matched to the highest-confidence rep. Returns
    (selected, leftovers) where leftovers are distinct-but-over-target
    candidates that can back-fill the other gender.
    """
    selected: list[dict] = []
    sel_clips: list[bytes] = []
    leftovers: list[dict] = []
    for rec in recs:
        clip = _clip_path(rec["key"]).read_bytes()
        if not selected:
            selected.append(rec); sel_clips.append(clip)
            rec["selected"] = True; rec["duplicate_of"] = None
            rec["dedup_confidence"] = None
            print(f"  [{label}] SELECT {rec['key']}  (first)", flush=True)
            continue
        matches = await asyncio.gather(*[
            _pair_match(genai_client, sem, a=ref, b=clip) for ref in sel_clips])
        best_i, best_c = -1, 0.0
        for i, m in enumerate(matches):
            if m.same_speaker and m.confidence >= DUP_CONFIDENCE and m.confidence > best_c:
                best_i, best_c = i, m.confidence
        if best_i >= 0:
            dup_of = selected[best_i]["key"]
            rec["selected"] = False; rec["duplicate_of"] = dup_of
            rec["dedup_confidence"] = best_c
            print(f"  [{label}] dup    {rec['key']}  == {dup_of} "
                  f"(conf {best_c:.2f})", flush=True)
        elif len(selected) < target:
            selected.append(rec); sel_clips.append(clip)
            rec["selected"] = True; rec["duplicate_of"] = None
            rec["dedup_confidence"] = None
            print(f"  [{label}] SELECT {rec['key']}  (distinct)", flush=True)
        else:
            rec["selected"] = False; rec["duplicate_of"] = None
            rec["dedup_confidence"] = None
            leftovers.append(rec)
    return selected, leftovers


async def _run(female_target: int, male_target: int, budget: int) -> int:
    settings = load_settings()
    recs = json.loads(OUT_JSON.read_text())
    # Clear any prior (music-contaminated) dedup decision — full recompute.
    for r in recs:
        r["selected"] = False
        r["duplicate_of"] = None
        r["dedup_confidence"] = None

    from elevenlabs.client import AsyncElevenLabs
    from google import genai
    el_client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
    genai_client = genai.Client(api_key=settings.gemini_api_key)

    # Isolate speech (strip the shared music bed) for every sample, cached.
    print(f"Isolating speech for {len(recs)} samples (ElevenLabs)…")
    iso_sem = asyncio.Semaphore(ISOLATE_CONCURRENCY)
    results = await asyncio.gather(*[
        _isolate_clip(el_client, iso_sem,
                      STAGE_DIR / r["platform"] / r["source_file"],
                      _clip_path(r["key"]))
        for r in recs
    ])
    errs = [(r["key"], e) for r, e in zip(recs, results) if e]
    if errs:
        for k, why in errs:
            print(f"  isolate FAILED {k}: {why}", file=sys.stderr)

    usable = [r for r in recs if _clip_path(r["key"]).exists()
              and _clip_path(r["key"]).stat().st_size > 0]
    female = sorted([r for r in usable
                     if (r["characteristics"]["gender"] or "").lower().startswith("f")],
                    key=_quality_key)
    male = sorted([r for r in usable
                   if (r["characteristics"]["gender"] or "").lower().startswith("m")],
                  key=_quality_key)
    print(f"{len(usable)} usable samples: {len(female)} female, {len(male)} male. "
          f"Targets: {female_target}F / {male_target}M, budget {budget}.")

    sem = asyncio.Semaphore(GEMINI_CONCURRENCY)

    print("\nDeduping on isolated speech, pairwise (female ∥ male buckets)…")
    (fsel, fleft), (msel, mleft) = await asyncio.gather(
        _select_bucket(genai_client, sem, recs=female, target=female_target, label="F"),
        _select_bucket(genai_client, sem, recs=male, target=male_target, label="M"),
    )

    selected = fsel + msel
    # Back-fill toward budget from distinct leftovers (female first — lean female).
    for rec in (fleft + mleft):
        if len(selected) >= budget:
            break
        rec["selected"] = True
        selected.append(rec)
        print(f"  [fill] SELECT {rec['key']}")

    sel_keys = {r["key"] for r in selected}
    for r in recs:
        if r["key"] not in sel_keys and r.get("selected"):
            r["selected"] = False
    OUT_JSON.write_text(json.dumps(recs, indent=2, ensure_ascii=False))

    import collections
    gsel = collections.Counter(r["characteristics"]["gender"] for r in selected)
    print(f"\nSelected {len(selected)} distinct voices → {dict(gsel)}")
    dups = sum(1 for r in recs if r.get("duplicate_of"))
    print(f"Flagged {dups} duplicates. Written → {OUT_JSON}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--female", type=int, default=18)
    ap.add_argument("--male", type=int, default=12)
    ap.add_argument("--budget", type=int, default=30)
    args = ap.parse_args()
    return asyncio.run(_run(args.female, args.male, args.budget))


if __name__ == "__main__":
    raise SystemExit(main())
