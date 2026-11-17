"""V5 stress-test on the winning combo (seedream-4-5-251128 + revised prompt).

Fixes over V4:
  - 3-arm anatomy bug: drop conflicting "extended selfie arm" framing; both
    hands hold the bowl; explicit "two arms total" constraint + stronger
    negative against extra limbs.
  - Freckles bias: Indians rarely have freckles; remove any micro-skin-mark
    invitations, add explicit negative.
  - Gender parity: parallel male prompt with the same demographic +
    photoreal anchors.

Fires 4 variants in parallel on seedream-4-5-251128:
  v5a_female  v5b_female  (reproducibility check)
  v5c_male    v5d_male    (gender parity + reproducibility)

Usage:  uv run python scripts/test_v5_grid.py
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
MODEL = "seedream-4-5-251128"

# ── V5 FEMALE ──────────────────────────────────────────────────────────────
# Fixes vs V4: removed "extended selfie arm" framing (was conflicting with
# "holds bowl" → 3 arms), both hands now on bowl, explicit "two arms total"
# constraint, freckles/skin-marks explicitly negated, indian-skin uniformity
# emphasised.
PROMPT_FEMALE = (
    "Hyperrealistic UGC paused-video-frame photo of a 23-year-old urban "
    "North Indian woman with fair-to-medium golden-undertone skin and "
    "light wheatish complexion (NOT dark, NOT honey-brown, NOT tanned, NOT "
    "South Indian deep brown), even-toned healthy Indian skin with a "
    "natural healthy glow, no makeup. Normal real-human Indian face "
    "proportions with slight natural asymmetry — eyes natural size (not "
    "enlarged), soft natural cheekbones (not sculpted), natural jawline "
    "(not sharpened), faint sleep-shadow under the eyes. Even uniform "
    "Indian skin tone across the face — NO freckles, NO moles, NO spots, "
    "NO scattered skin marks, NO heavy texture (Indians rarely have "
    "freckles). Naturally full eyebrows, soft warm brown eyes with "
    "natural lashes, a small modern gold septum nose ring, a relaxed "
    "candid mid-laugh expression caught mid-sentence (not posed, not "
    "smiling-for-camera). Long dark-brown hair in a slightly imperfect "
    "messy high bun with a few wispy strands at the temples. Wearing a "
    "fitted cream-beige ribbed cotton crew tank, two layered delicate "
    "gold chains (one with a tiny heart pendant), small gold huggie hoop "
    "earrings, a simple gold ring on her index finger. She holds a white "
    "natural-clay ceramic bowl of creamy oats topped with sliced banana, "
    "blueberries, honey drizzle and chia seeds — BOTH of her hands are "
    "on the bowl, lifting it in front of her chest toward the camera. "
    "Exactly TWO arms total visible in the frame, NO extra arm reaching "
    "out, NO third arm, NO duplicated limb. Standing at a marble counter "
    "in a modern Mumbai apartment kitchen; the cream-and-green Goat Life "
    "Oats packet sits on the counter behind her crisp and legible, a "
    "small money plant on the windowsill camera-left, a stainless-steel "
    "electric kettle in soft focus, the background sharp and readable, "
    "not blurred. Shot on iPhone 15 Pro back camera held by a friend at "
    "chest height, JPEG compression, slightly underexposed by half a "
    "stop, ambient indoor light not colour-corrected, natural "
    "phone-camera colour cast, NOT portrait mode, NOT studio, NOT "
    "cinematic. Soft natural daylight from a window camera-left plus "
    "warm overhead LED ceiling light filling in, slightly flat "
    "illumination across her face. Vertical 9:16, waist-up framing, "
    "slightly off-center natural composition, candid documentary "
    "phone-snapshot aesthetic, paused-video-frame look. No glossy AI "
    "face, no waxy plastic skin, no perfect symmetry, no portrait-mode "
    "background blur, no fake bokeh, no rim light, no golden hair-edge "
    "glow, no studio cinema lighting, no beauty-filter smoothing, no "
    "over-processed AI rendering, no enlarged anime eyes, no sharpened "
    "AI jawline, no stock-photo energy, no overly dark complexion, no "
    "South Indian stereotyping, no freckles, no moles, no spots, no skin "
    "marks. ANATOMY: exactly two arms, two hands, ten fingers total — "
    "no third arm, no duplicated arm, no extra hand, no extra limb."
)

# ── V5 MALE ────────────────────────────────────────────────────────────────
# Parallel structure: same demographic, same setting, same lighting and
# gear anchor. Swap jewelry-heavy female styling for understated male
# styling: light stubble, single thin gold chain, no earrings/septum,
# short modern haircut.
PROMPT_MALE = (
    "Hyperrealistic UGC paused-video-frame photo of a 25-year-old urban "
    "North Indian man with fair-to-medium golden-undertone skin and "
    "light wheatish complexion (NOT dark, NOT honey-brown, NOT tanned, "
    "NOT South Indian deep brown), even-toned healthy Indian skin with a "
    "natural healthy glow. Normal real-human Indian male face "
    "proportions with slight natural asymmetry — eyes natural size, soft "
    "natural cheekbones, natural jawline (not sharpened, not square-cut), "
    "faint sleep-shadow under the eyes. Even uniform Indian skin tone "
    "across the face — NO freckles, NO moles, NO spots, NO scattered "
    "skin marks (Indians rarely have freckles). Naturally full dark "
    "eyebrows, warm dark-brown eyes with natural lashes, light short "
    "stubble (3-4 day growth, not a full beard), a relaxed candid "
    "mid-laugh expression caught mid-sentence (not posed, not "
    "smiling-for-camera). Short modern dark-brown hair, slightly tousled, "
    "neither gelled-and-styled nor unkempt. Wearing a fitted plain "
    "cream-beige cotton crew-neck t-shirt, a single thin delicate gold "
    "chain at the neckline, no earrings, no septum, no rings visible. "
    "He holds a white natural-clay ceramic bowl of creamy oats topped "
    "with sliced banana, blueberries, honey drizzle and chia seeds — "
    "BOTH of his hands are on the bowl, lifting it in front of his "
    "chest toward the camera. Exactly TWO arms total visible in the "
    "frame, NO extra arm reaching out, NO third arm, NO duplicated "
    "limb. Standing at a marble counter in a modern Mumbai apartment "
    "kitchen; the cream-and-green Goat Life Oats packet sits on the "
    "counter behind him crisp and legible, a small money plant on the "
    "windowsill camera-left, a stainless-steel electric kettle in soft "
    "focus, the background sharp and readable, not blurred. Shot on "
    "iPhone 15 Pro back camera held by a friend at chest height, JPEG "
    "compression, slightly underexposed by half a stop, ambient indoor "
    "light not colour-corrected, natural phone-camera colour cast, NOT "
    "portrait mode, NOT studio, NOT cinematic. Soft natural daylight "
    "from a window camera-left plus warm overhead LED ceiling light "
    "filling in, slightly flat illumination across his face. Vertical "
    "9:16, waist-up framing, slightly off-center natural composition, "
    "candid documentary phone-snapshot aesthetic, paused-video-frame "
    "look. No glossy AI face, no waxy plastic skin, no perfect symmetry, "
    "no portrait-mode background blur, no fake bokeh, no rim light, no "
    "golden hair-edge glow, no studio cinema lighting, no beauty-filter "
    "smoothing, no over-processed AI rendering, no sharpened AI "
    "jawline, no stock-photo energy, no overly dark complexion, no "
    "South Indian stereotyping, no freckles, no moles, no spots, no "
    "skin marks, no full thick beard. ANATOMY: exactly two arms, two "
    "hands, ten fingers total — no third arm, no duplicated arm, no "
    "extra hand, no extra limb."
)


@dataclass(frozen=True)
class Variant:
    name: str
    prompt: str


VARIANTS: list[Variant] = [
    Variant("v5a_female", PROMPT_FEMALE),
    Variant("v5b_female", PROMPT_FEMALE),
    Variant("v5c_male",   PROMPT_MALE),
    Variant("v5d_male",   PROMPT_MALE),
]


def call_api(variant: Variant, api_key: str) -> dict:
    body = json.dumps({
        "model": MODEL,
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
        raise RuntimeError(f"HTTP {e.code} on {MODEL}: {msg}") from None


def run_variant(variant: Variant, api_key: str, out_dir: Path) -> tuple[str, str | None, str | None]:
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
            f"model: {MODEL}\nseconds: {time.time()-t0:.1f}\nurl: {url}\n\n"
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
    out_dir = ROOT / "generated-images" / f"v5-grid-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {out_dir}\n")
    print(f"Firing {len(VARIANTS)} variants in parallel on {MODEL}...")
    for v in VARIANTS:
        print(f"  - {v.name:14s} prompt_words={len(v.prompt.split())}")
    print()

    with ThreadPoolExecutor(max_workers=len(VARIANTS)) as ex:
        futures = {ex.submit(run_variant, v, api_key, out_dir): v for v in VARIANTS}
        for fut in as_completed(futures):
            name, path, err = fut.result()
            status = "OK  " if path else "FAIL"
            print(f"  [{status}] {name}: {path or err}")

    print(f"\nAll outputs in: {out_dir}")


if __name__ == "__main__":
    main()
