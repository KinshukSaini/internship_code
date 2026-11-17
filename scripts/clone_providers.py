"""Clone the selected voice-library samples into Cartesia and/or Fish Audio.

The ElevenLabs clones already exist (scripts/clone_selected.py). This does the
same job for the other two providers so the SAME real influencer voice is
available on every engine — data/voice_clones/voices.json keeps one id column
per provider (`elevenlabs_voice_id` / `cartesia_voice_id` / `fish_voice_id`),
keyed on the record's `key`, so switching providers never changes who is
speaking.

Source audio comes from each record's `s3_url` (the original staged sample on
R2), so this does not depend on the /tmp staging dir clone_selected.py used.

Clip-window selection matters and is NOT a blind head slice: both providers want
a SHORT clip (Cartesia hard-caps at ~10s; Fish recommends 10-30s), and Cartesia
warns that long pauses inside the clip get MIMICKED by the clone. So we probe
with ffmpeg silencedetect and cut from the longest continuous speech run.

Resumable / safe to re-run: a record with an id for the target provider is
skipped unless --force. Failures are recorded per-provider in `clone_errors` and
never abort the batch.

    uv run python scripts/clone_providers.py --provider cartesia --dry-run
    uv run python scripts/clone_providers.py --provider cartesia
    uv run python scripts/clone_providers.py --provider fish
    uv run python scripts/clone_providers.py --provider all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import (  # noqa: E402
    CARTESIA_API_VERSION,
    CARTESIA_CLONE_CLIP_S,
    CARTESIA_MAX_CONCURRENCY,
    FISH_API_BASE,
    FISH_CLONE_CLIP_S,
    FISH_MAX_CONCURRENCY,
    load_settings,
)
from app.voice_library import provider_id_field  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "data" / "voice_clones" / "voices.json"
CACHE_DIR = Path(tempfile.gettempdir()) / "clone_providers_cache"
CLONE_NAME_PREFIX = "OCLabs"

PROVIDERS = ("cartesia", "fish")


# ── source audio ─────────────────────────────────────────────────────────────

def _clone_name(rec: dict) -> str:
    """Stable, human-readable clone name — mirrors clone_selected.py so the same
    voice carries the same name across providers."""
    m = re.match(r"\s*(\d+)", Path(rec["source_file"]).stem)
    slug = m.group(1) if m else str(rec.get("row", 0))
    return f"{CLONE_NAME_PREFIX}-{rec['platform']}-{slug}"


def _cache_path(rec: dict) -> Path:
    """Local cache for the downloaded original (shared across providers so a
    two-provider run downloads each sample once)."""
    return CACHE_DIR / f"{Path(rec['s3_url']).name}"


def _download(rec: dict) -> Path:
    dst = _cache_path(rec)
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    import httpx
    with httpx.Client(timeout=120.0, follow_redirects=True) as c:
        r = c.get(rec["s3_url"])
        r.raise_for_status()
        dst.write_bytes(r.content)
    return dst


def _speech_start(src: Path) -> float:
    """Start of the longest continuous SPEECH run, in seconds.

    Uses ffmpeg silencedetect to find silences, inverts them into speech runs,
    and returns the start of the longest. A clip cut mid-pause clones the pause
    (Cartesia's docs are explicit that pauses get mimicked), and the head of a
    UGC sample is often an intro sting or music. Returns 0.0 if detection finds
    nothing usable — the caller still gets a valid window.
    """
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(src), "-af",
         # -35dB is permissive enough for soft-spoken UGC without treating
         # background music as speech; 0.35s ignores inter-word gaps.
         "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = r.stderr or ""
    total = 0.0
    # ffmpeg prints a running "time=HH:MM:SS.ms"; the LAST one is the duration.
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", log)
    if times:
        h, m_, s_ = times[-1]
        total = int(h) * 3600 + int(m_) * 60 + float(s_)
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", log)]
    if not starts and not ends:
        return 0.0
    # Speech runs are the gaps BETWEEN silences: [0,starts[0]], then
    # [ends[i], starts[i+1]], ... and finally [ends[-1], total].
    bounds: list[tuple[float, float]] = []
    if starts:
        bounds.append((0.0, starts[0]))
    for i, e in enumerate(ends):
        nxt = starts[i + 1] if i + 1 < len(starts) else (total or e)
        if nxt > e:
            bounds.append((e, nxt))
    if not bounds:
        return 0.0
    best = max(bounds, key=lambda b: b[1] - b[0])
    return max(0.0, best[0])


def _clone_clip(src: Path, *, seconds: float) -> bytes:
    """mono 44.1k mp3 of `seconds` from the longest speech run."""
    start = _speech_start(src)
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}",
         "-i", str(src), "-vn", "-ac", "1", "-ar", "44100", "-b:a", "192k",
         "-f", "mp3", "pipe:1"],
        check=True, capture_output=True,
    )
    if not r.stdout:
        raise RuntimeError("ffmpeg produced an empty clone clip")
    return r.stdout


# ── providers ────────────────────────────────────────────────────────────────

async def _clone_cartesia(client, rec: dict, audio: bytes) -> str:
    """POST /voices/clone (multipart). Returns the new voice id.

    NOTE: `mode`, `enhance` and `transcript` do NOT exist on the current
    endpoint (they lived on the /voices/clone/clip route that was removed
    2026-06-01) — sending them 422s.
    """
    ch = rec.get("characteristics") or {}
    resp = await client.voices.clone(
        clip=(f"{_clone_name(rec)}.mp3", audio, "audio/mpeg"),
        name=_clone_name(rec),
        language="en",
        description=(ch.get("summary") or "")[:500] or None,
    )
    vid = getattr(resp, "id", None) or (
        resp.get("id") if isinstance(resp, dict) else None)
    if not vid:
        raise RuntimeError(f"no voice id in Cartesia response: {resp!r}")
    return vid


async def _clone_fish(client, api_key: str, rec: dict, audio: bytes) -> str:
    """POST /model (multipart). Returns the new model `_id`.

    visibility MUST be sent explicitly: the API defaults to `public`, and public
    models also require a cover_image. train_mode="fast" is the only legal value
    and makes the model available immediately.
    """
    ch = rec.get("characteristics") or {}
    # `data` MUST be a dict here. httpx treats a list-of-tuples as raw request
    # CONTENT (the deprecated path), which builds a *sync* byte stream and then
    # dies with "Attempted to send an sync request with an AsyncClient
    # instance". Repeated form fields are expressed as list VALUES instead.
    data: dict[str, object] = {
        "type": "tts",
        "train_mode": "fast",
        "title": _clone_name(rec),
        "visibility": "private",
        "enhance_audio_quality": "true",
    }
    desc = (ch.get("summary") or "")[:500]
    if desc:
        data["description"] = desc
    tags = [str(t) for t in (ch.get("gender"), ch.get("age_band"),
                             ch.get("voice_vibe")) if t]
    if tags:
        data["tags"] = tags
    resp = await client.post(
        "/model",
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
        files=[("voices", (f"{_clone_name(rec)}.mp3", audio, "audio/mpeg"))],
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"HTTP {resp.status_code}: {(resp.text or '')[:300]}")
    body = resp.json()
    vid = body.get("_id")
    if not vid:
        raise RuntimeError(f"no _id in Fish response: {str(body)[:200]}")
    state = body.get("state")
    if state not in (None, "trained", "created"):
        raise RuntimeError(f"clone state={state!r} (quality gate may have failed)")
    return vid


# ── driver ───────────────────────────────────────────────────────────────────

def _record_error(rec: dict, provider: str, msg: str | None) -> None:
    errs = rec.setdefault("clone_errors", {})
    errs[provider] = msg


async def _run(providers: list[str], dry_run: bool, force: bool,
               limit: int | None) -> int:
    settings = load_settings()
    recs = json.loads(OUT_JSON.read_text())
    lock = asyncio.Lock()
    rc = 0

    for provider in providers:
        id_field = provider_id_field(provider)
        clip_s = (CARTESIA_CLONE_CLIP_S if provider == "cartesia"
                  else FISH_CLONE_CLIP_S)
        conc = (CARTESIA_MAX_CONCURRENCY if provider == "cartesia"
                else FISH_MAX_CONCURRENCY)
        todo = [r for r in recs
                if r.get("selected") and (force or not r.get(id_field))]
        if limit:
            todo = todo[:limit]
        n_sel = sum(1 for r in recs if r.get("selected"))
        print(f"\n=== {provider} ===")
        print(f"{n_sel} selected; {len(todo)} to clone "
              f"(clip={clip_s:g}s, concurrency={conc})")
        if dry_run:
            for r in todo:
                print(f"  [dry-run] would clone {_clone_name(r)}  <- {r['key']}")
            continue
        if not todo:
            print("Nothing to clone.")
            continue

        if provider == "cartesia":
            if not settings.cartesia_api_key:
                print("  SKIP: CARTESIA_API_KEY is not set")
                rc = 1
                continue
            from cartesia import AsyncCartesia
            client = AsyncCartesia(
                api_key=settings.cartesia_api_key,
                default_headers={"Cartesia-Version": CARTESIA_API_VERSION},
                max_retries=3,
            )
            closer = getattr(client, "close", None)
        else:
            if not settings.fish_api_key:
                print("  SKIP: FISH_API_KEY is not set")
                rc = 1
                continue
            import httpx
            client = httpx.AsyncClient(base_url=FISH_API_BASE, timeout=300.0)
            closer = client.aclose

        sem = asyncio.Semaphore(max(1, conc))

        async def _worker(rec: dict, provider=provider, client=client,
                          clip_s=clip_s, id_field=id_field) -> None:
            try:
                src = await asyncio.to_thread(_download, rec)
                audio = await asyncio.to_thread(_clone_clip, src, seconds=clip_s)
                async with sem:
                    if provider == "cartesia":
                        vid = await _clone_cartesia(client, rec, audio)
                    else:
                        vid = await _clone_fish(
                            client, settings.fish_api_key, rec, audio)
                rec[id_field] = vid
                _record_error(rec, provider, None)
                print(f"  cloned {_clone_name(rec):<18} {vid}  <- {rec['key']}")
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                _record_error(rec, provider, msg)
                print(f"  ERROR {rec['key']}: {msg[:140]}")
            async with lock:
                OUT_JSON.write_text(
                    json.dumps(recs, indent=2, ensure_ascii=False))

        try:
            await asyncio.gather(*[_worker(r) for r in todo])
        finally:
            if closer is not None:
                res = closer()
                if asyncio.iscoroutine(res):
                    await res

        done = sum(1 for r in recs if r.get("selected") and r.get(id_field))
        print(f"{provider}: {done}/{n_sel} selected voices cloned.")
        if done < n_sel:
            rc = rc or 0  # partial coverage is tolerated; the matcher scopes itself

    if not dry_run:
        print(f"\nWritten → {OUT_JSON}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", choices=(*PROVIDERS, "all"), required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-clone even if an id already exists")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N records (smoke test)")
    args = ap.parse_args()
    providers = list(PROVIDERS) if args.provider == "all" else [args.provider]
    return asyncio.run(_run(providers, args.dry_run, args.force, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
