"""Cartesia + Fish TTS smoke test — full voice-client contract (TTS +
ElevenLabs forced alignment) on a short line, so provider breakage shows up
before a paid video run depends on it.

Usage: .venv/bin/python scratch-probe/tts_smoke.py
"""
import asyncio
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings          # noqa: E402
from app.voice import make_voice_client       # noqa: E402

SCRIPT = "Okay so this serum genuinely changed my mornings. Two drops, done."
OUT = ROOT / "scratch-probe" / "tts-smoke"


def _fish_voice_id() -> str | None:
    recs = json.load(open(ROOT / "data/voice_clones/voices.json"))
    for r in recs:
        if r.get("selected") and r.get("fish_voice_id"):
            return r["fish_voice_id"]
    return None


async def _cartesia_stock_voice(settings) -> str | None:
    import httpx
    from app.config import CARTESIA_API_VERSION
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "https://api.cartesia.ai/voices/",
            headers={"X-API-Key": settings.cartesia_api_key,
                     "Cartesia-Version": CARTESIA_API_VERSION},
            params={"limit": 5},
        )
        r.raise_for_status()
        data = r.json()
        voices = data.get("data") or data
        return voices[0]["id"] if voices else None


async def smoke(provider: str, voice_id: str, settings) -> None:
    print(f"\n=== {provider} (voice {voice_id}) ===", flush=True)
    client = make_voice_client(settings, provider)
    out_path = OUT / f"{provider}.mp3"
    vo = await client.generate_vo(
        script=SCRIPT, voice_id=voice_id, out_path=out_path,
        voice_vibe="warm_energetic",
    )
    al = vo.alignment
    size = Path(vo.audio_path).stat().st_size
    print(f"OK: {size} bytes, {al.duration:.2f}s, "
          f"{len(al.characters)} aligned chars", flush=True)
    joined = "".join(al.characters)
    assert size > 10_000, "suspiciously small audio"
    assert al.duration > 1.0, "suspiciously short audio"
    assert "serum" in joined, f"alignment text drifted: {joined[:80]!r}"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    failures = []

    fish_id = _fish_voice_id()
    if fish_id:
        try:
            await smoke("fish", fish_id, settings)
        except Exception:
            failures.append("fish")
            traceback.print_exc()
    else:
        print("fish: no cloned voice id available — SKIP")

    try:
        cart_id = await _cartesia_stock_voice(settings)
        if cart_id:
            await smoke("cartesia", cart_id, settings)
        else:
            print("cartesia: no stock voice found — SKIP")
    except Exception:
        failures.append("cartesia")
        traceback.print_exc()

    print(f"\nRESULT: {'FAIL ' + ','.join(failures) if failures else 'ALL OK'}")


if __name__ == "__main__":
    asyncio.run(main())
