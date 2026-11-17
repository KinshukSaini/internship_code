"""Generate the UGC character image via Gemini Nano Banana Pro.

Why Nano Banana Pro for this script: this is the canonical "hero"
character reference. Pro gives the strongest pure photorealism
(girlfriend-selfie aesthetic that fools a human glance) — worth the
~4× cost over Nano Banana 2 for a single, reused identity anchor.

The workflow pipeline uses Nano Banana 2 for the subsequent N+1 anchor
frames per video — see app/image_gen.py.

Usage:
    uv run python scripts/gen_character.py            # female (default)
    uv run python scripts/gen_character.py --gender male
    uv run python scripts/gen_character.py --gender female -n 2

Saves to generated-images/<gender>-<timestamp>-<i>.jpg.

Note on prompts: the in-file prompts below were written for Seedream
5.0/4.5 and use a lot of "no X, no Y" negative phrasing. Nano Banana
treats those weakly — Google's prompting guide explicitly says "Describe
what you want, not what you don't want." If you find Nano Banana ignoring
the negations, rewrite the relevant sections as positive descriptors
("uniform Indian skin tone" instead of "no freckles, no moles, no spots").
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

MODEL = "gemini-3-pro-image-preview"
API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
)

# ── FEMALE PROMPT (V5b winner, with "friend" wording removed) ──────────────
PROMPT_FEMALE = (
    "Hyperrealistic UGC paused-video-frame photo of a 23-year-old urban "
    "North Indian woman with fair-to-medium golden-undertone skin and "
    "light wheatish complexion (NOT dark, NOT honey-brown, NOT tanned, "
    "NOT South Indian deep brown), even-toned healthy Indian skin with a "
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
    "out, NO third arm, NO duplicated limb, NO ghost arm at the edge of "
    "the frame. Standing at a marble counter in a modern Mumbai "
    "apartment kitchen; the cream-and-green Goat Life Oats packet sits "
    "on the counter behind her crisp and legible, a small money plant "
    "on the windowsill camera-left, a stainless-steel electric kettle "
    "in soft focus, the background sharp and readable, not blurred. "
    "Shot on an iPhone 15 Pro back camera at chest height, JPEG "
    "compression, slightly underexposed by half a stop, ambient indoor "
    "light not colour-corrected, natural phone-camera colour cast, NOT "
    "portrait mode, NOT studio, NOT cinematic. Soft natural daylight "
    "from a window camera-left plus warm overhead LED ceiling light "
    "filling in, slightly flat illumination across her face. Vertical "
    "9:16, waist-up framing, slightly off-center natural composition, "
    "candid documentary phone-snapshot aesthetic, paused-video-frame "
    "look. No glossy AI face, no waxy plastic skin, no perfect "
    "symmetry, no portrait-mode background blur, no fake bokeh, no rim "
    "light, no golden hair-edge glow, no studio cinema lighting, no "
    "beauty-filter smoothing, no over-processed AI rendering, no "
    "enlarged anime eyes, no sharpened AI jawline, no stock-photo "
    "energy, no overly dark complexion, no South Indian stereotyping, "
    "no freckles, no moles, no spots, no skin marks. ANATOMY: exactly "
    "two arms, two hands, ten fingers total — no third arm, no "
    "duplicated arm, no extra hand, no extra limb, no ghost arm."
)

# ── MALE PROMPT (V5d structure, with harder earring negation) ──────────────
PROMPT_MALE = (
    "Hyperrealistic UGC paused-video-frame photo of a 25-year-old urban "
    "North Indian man with fair-to-medium golden-undertone skin and "
    "light wheatish complexion (NOT dark, NOT honey-brown, NOT tanned, "
    "NOT South Indian deep brown), even-toned healthy Indian skin with a "
    "natural healthy glow. Normal real-human Indian male face "
    "proportions with slight natural asymmetry — eyes natural size, soft "
    "natural cheekbones, natural jawline (not sharpened, not "
    "square-cut), faint sleep-shadow under the eyes. Even uniform "
    "Indian skin tone across the face — NO freckles, NO moles, NO "
    "spots, NO scattered skin marks (Indians rarely have freckles). "
    "Naturally full dark eyebrows, warm dark-brown eyes with natural "
    "lashes, light short stubble (3-4 day growth, not a full beard), a "
    "relaxed candid mid-laugh expression caught mid-sentence (not "
    "posed, not smiling-for-camera). Short modern dark-brown hair, "
    "slightly tousled, neither gelled-and-styled nor unkempt. EARS: "
    "completely bare ears, NO ear piercings of any kind, NO studs, NO "
    "hoops, NO earrings, NO black stud, NO gold stud — both ears must "
    "be unpierced and unadorned. Wearing a fitted plain cream-beige "
    "cotton crew-neck t-shirt, a single thin delicate gold chain at the "
    "neckline, NO rings on the fingers. He holds a white natural-clay "
    "ceramic bowl of creamy oats topped with sliced banana, "
    "blueberries, honey drizzle and chia seeds — BOTH of his hands are "
    "on the bowl, lifting it in front of his chest toward the camera. "
    "Exactly TWO arms total visible in the frame, NO extra arm reaching "
    "out, NO third arm, NO duplicated limb, NO ghost arm at the edge "
    "of the frame. Standing at a marble counter in a modern Mumbai "
    "apartment kitchen; the cream-and-green Goat Life Oats packet sits "
    "on the counter behind him crisp and legible, a small money plant "
    "on the windowsill camera-left, a stainless-steel electric kettle "
    "in soft focus, the background sharp and readable, not blurred. "
    "Shot on an iPhone 15 Pro back camera at chest height, JPEG "
    "compression, slightly underexposed by half a stop, ambient indoor "
    "light not colour-corrected, natural phone-camera colour cast, NOT "
    "portrait mode, NOT studio, NOT cinematic. Soft natural daylight "
    "from a window camera-left plus warm overhead LED ceiling light "
    "filling in, slightly flat illumination across his face. Vertical "
    "9:16, waist-up framing, slightly off-center natural composition, "
    "candid documentary phone-snapshot aesthetic, paused-video-frame "
    "look. No glossy AI face, no waxy plastic skin, no perfect "
    "symmetry, no portrait-mode background blur, no fake bokeh, no rim "
    "light, no golden hair-edge glow, no studio cinema lighting, no "
    "beauty-filter smoothing, no over-processed AI rendering, no "
    "sharpened AI jawline, no stock-photo energy, no overly dark "
    "complexion, no South Indian stereotyping, no freckles, no moles, "
    "no spots, no skin marks, no full thick beard, no ear piercings, "
    "no earrings. ANATOMY: exactly two arms, two hands, ten fingers "
    "total — no third arm, no duplicated arm, no extra hand, no extra "
    "limb, no ghost arm."
)

PROMPTS = {"female": PROMPT_FEMALE, "male": PROMPT_MALE}


def call(prompt: str, n: int = 1) -> list[bytes]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY missing from .env")

    images: list[bytes] = []
    for _ in range(n):
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "9:16", "imageSize": "2K"},
            },
        }).encode()
        req = Request(
            f"{API_URL}?key={api_key}", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            raise RuntimeError(
                f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
            ) from None

        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if not inline or not inline.get("data"):
                    continue
                images.append(base64.b64decode(inline["data"]))
    return images


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate UGC character image (Nano Banana Pro)",
    )
    parser.add_argument(
        "--gender", choices=["female", "male"], default="female",
        help="Which gender prompt to use (default: female)",
    )
    parser.add_argument(
        "-n", "--num", type=int, default=1,
        help="Number of images to generate (default: 1)",
    )
    args = parser.parse_args()

    prompt = PROMPTS[args.gender]
    print(f"Model: {MODEL}")
    print(f"Gender: {args.gender}")
    print(f"Generating {args.num} image(s)...\n")

    images = call(prompt, n=args.num)

    out_dir = ROOT / "generated-images"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    for i, img_bytes in enumerate(images, start=1):
        suffix = f"-{i}" if args.num > 1 else ""
        out_path = out_dir / f"{args.gender}-{ts}{suffix}.jpg"
        out_path.write_bytes(img_bytes)
        print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
