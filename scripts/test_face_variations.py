"""A/B grid: Seedream 5.0 vs 4.5  x  baseline prompt vs revised prompt.

Goal: find the model+prompt combo that produces faces matching the user's
real-creator references (fair-to-medium golden urban North Indian skin,
natural mid-laugh expression, not the dark/stiff/AI-glossy default).

Usage:  uv run python scripts/test_face_variations.py
Outputs: generated-images/face-test-<ts>/<variant>.png  + <variant>.txt (prompt)
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

API_URL = "https://ark.ap-southeast.bytepluses.com/api/v3/images/generations"
MODEL_50 = "seedream-5-0-260128"
MODEL_45 = "seedream-4-5-251128"

# ── BASELINE: current prompt from scripts/gen_character.py, verbatim ────────
BASELINE = (
    "Hyperrealistic UGC paused-video-frame selfie of an Indian woman in her "
    "mid-twenties with natural curly shoulder-length dark brown hair, a "
    "friendly approachable faint half-smile, soft natural skin with visible "
    "pores, peach fuzz and mild micro-asymmetry, no beauty filter. She wears "
    "a fitted cream-beige ribbed cotton crew-neck t-shirt, a delicate gold "
    "chain with a small heart pendant, and a single small gold huggie hoop "
    "earring. She holds a white natural-clay ceramic bowl of creamy oats "
    "topped with sliced banana, fresh blueberries, honey drizzle and chia "
    "seeds, presenting it gently toward the camera. She stands at a marble "
    "counter in a modern Indian apartment kitchen; the cream-and-green Goat "
    "Life Oats packet sits on the counter behind her, a small money plant on "
    "the windowsill camera-left, a stainless steel electric kettle in soft "
    "focus. Lighting: cool natural daylight from the window camera-left plus "
    "warm overhead LED fill, not color-corrected. Composition: vertical 9:16 "
    "medium close-up matching a handheld arm-extended iPhone selfie, slightly "
    "off-center natural framing, sharp focus on her eyes and skin texture, "
    "kitchen background in soft focus, 85mm-equivalent lens feel. Candid "
    "documentary photography aesthetic, paused-video-frame look, not "
    "cinematic and not studio. Goat Life Oats packaging crisp and legible. "
    "No beauty-filter smoothing, no Instagram UI overlays, no text overlay, "
    "no watermark, no extra fingers."
)

# ── REVISED: fixes skin-tone bias + face stiffness, adds iPhone anchor ─────
# Targeted at the look in the user's reference images (fair-to-medium urban
# North Indian creator complexion, candid mid-laugh, not the AI-glossy
# default).
REVISED = (
    "Hyperrealistic UGC paused-video-frame selfie of a 23-year-old urban "
    "North Indian woman with fair-to-medium golden-undertone skin and light "
    "wheatish complexion (NOT dark, NOT honey-brown, NOT tanned), natural "
    "healthy glow without makeup. Normal real-human face proportions with "
    "slight natural asymmetry — eyes natural size (not enlarged), soft "
    "natural cheekbones (not sculpted), natural jawline (not sharpened), "
    "faint sleep-shadow under the eyes, a few subtle skin variations across "
    "the cheeks, pores visible at natural scale. Naturally full eyebrows, "
    "soft warm brown eyes with natural lashes, a small modern gold septum "
    "nose ring, a relaxed candid mid-laugh expression caught mid-sentence "
    "(not posed, not smiling-for-camera). Long dark-brown hair in a "
    "slightly imperfect messy high bun with wispy strands escaping at the "
    "temples and a few baby hairs along the hairline. Wearing a fitted "
    "cream-beige ribbed cotton crew tank, two layered delicate gold chains "
    "(one with a tiny heart pendant), small gold huggie hoop earrings, a "
    "simple gold ring on her index finger. She holds a white natural-clay "
    "ceramic bowl of creamy oats topped with sliced banana, blueberries, "
    "honey drizzle and chia seeds in a relaxed natural grip — not stylised "
    "presentation. Standing at a marble counter in a modern Mumbai "
    "apartment kitchen; the cream-and-green Goat Life Oats packet sits "
    "behind her crisp and legible, a small money plant on the windowsill "
    "camera-left, a stainless-steel electric kettle in soft focus, the "
    "background sharp and readable not blurred. Shot on iPhone 15 Pro front "
    "camera, JPEG compression, slightly underexposed by half a stop, "
    "ambient indoor light not colour-corrected, natural phone-camera colour "
    "cast, NOT portrait mode, NOT studio, NOT cinematic. Soft natural "
    "daylight from a window camera-left plus warm overhead LED ceiling "
    "light filling in, slightly flat illumination across her face, gentle "
    "natural shadow on the camera-right side. Vertical 9:16, waist-up "
    "framing, slightly off-center natural composition, candid documentary "
    "phone-snapshot aesthetic, paused-video-frame look. No glossy AI face, "
    "no waxy plastic skin, no perfect symmetry, no portrait-mode background "
    "blur, no fake bokeh, no rim light, no golden hair-edge glow, no studio "
    "cinema lighting, no beauty-filter smoothing, no over-processed AI "
    "rendering, no enlarged anime eyes, no sharpened AI jawline, no "
    "stock-photo energy, no overly dark complexion, no South Indian "
    "stereotyping. Exactly two arms and two hands visible with correct "
    "finger count."
)


@dataclass(frozen=True)
class Variant:
    name: str
    model: str
    prompt: str


VARIANTS: list[Variant] = [
    Variant("v1_50_baseline", MODEL_50, BASELINE),
    Variant("v2_45_baseline", MODEL_45, BASELINE),
    Variant("v3_50_revised",  MODEL_50, REVISED),
    Variant("v4_45_revised",  MODEL_45, REVISED),
]


def call_api(variant: Variant, api_key: str) -> dict:
    body = json.dumps({
        "model": variant.model,
        "prompt": variant.prompt,
        "size": "1440x2560",
        "response_format": "url",
        "watermark": False,
        "n": 1,
    }).encode()
    req = Request(
        API_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {variant.model}: {msg}") from None


def run_variant(variant: Variant, api_key: str, out_dir: Path) -> tuple[str, str | None, str | None]:
    """Returns (variant_name, local_path_or_None, error_or_None)."""
    try:
        t0 = time.time()
        result = call_api(variant, api_key)
        url = result["data"][0].get("url")
        if not url:
            return variant.name, None, f"no url in response: {result}"
        out_path = out_dir / f"{variant.name}.png"
        with urlopen(url, timeout=180) as r, open(out_path, "wb") as f:
            f.write(r.read())
        (out_dir / f"{variant.name}.txt").write_text(
            f"model: {variant.model}\nseconds: {time.time()-t0:.1f}\nurl: {url}\n\n"
            f"PROMPT:\n{variant.prompt}\n"
        )
        return variant.name, str(out_path), None
    except Exception as e:
        return variant.name, None, str(e)


def main() -> None:
    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("ARK_API_KEY missing from .env")

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "generated-images" / f"face-test-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {out_dir}\n")

    print(f"Firing {len(VARIANTS)} variants in parallel...")
    for v in VARIANTS:
        print(f"  - {v.name:20s} model={v.model}  prompt_words={len(v.prompt.split())}")
    print()

    results: dict[str, tuple[str | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=len(VARIANTS)) as ex:
        futures = {ex.submit(run_variant, v, api_key, out_dir): v for v in VARIANTS}
        for fut in as_completed(futures):
            name, path, err = fut.result()
            results[name] = (path, err)
            status = "OK  " if path else "FAIL"
            print(f"  [{status}] {name}: {path or err}")

    print(f"\nAll outputs in: {out_dir}")


if __name__ == "__main__":
    main()
