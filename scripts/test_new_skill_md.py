"""End-to-end test of the identity-locked anchor generation flow.

Exercises the new Option B + C architecture:
  1. Write a CHARACTER REFERENCE prompt (Gemini Flash with skill MD +
     neutral-pose override) — for the canonical identity portrait.
  2. Render the character reference (NB Pro, hero tier) — composed pose.
  3. Write an ANCHOR FRAME prompt (Gemini Flash, chunk-action style).
  4. Render the anchor frame (NB2 anchor tier) WITH the character ref
     URL passed as a reference_image — identity should lock.

Saves both images locally so we can visually verify:
  - The character ref is a neutral identity portrait.
  - The anchor frame matches the character ref's face/hair/wardrobe
    while showing a different action.
"""
from __future__ import annotations

import asyncio
import time
import urllib.request
from pathlib import Path

from app.config import load_settings
from app.gemini import CharacterDescription, GeminiClient
from app.image_gen import make_image_client
from app.uploader import make_uploader

ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    settings = load_settings()
    uploader = make_uploader(settings)

    gem = GeminiClient(
        settings,
        image_skill_md_path=ROOT / "docs/character-image-skill.md",
        skill_md_path=ROOT / "video-seedance-skill.md",
    )
    seedr = make_image_client(settings, uploader)

    char = CharacterDescription(
        age=23,
        gender="female",
        ethnicity="Indian",
        region="north_indian",
        skin_tone="fair_golden",
        hair_style="long_loose_natural_waves",
        setting="apartment_living_couch",
        expression="calm_mid_sentence",
        hair=(
            "long jet-black hair worn down with natural soft waves, "
            "parted naturally, baby hairs visible at the temples"
        ),
        face=(
            "warm brown eyes with naturally full lashes, strong "
            "naturally full eyebrows kept naturally arched"
        ),
        wardrobe=(
            "fitted dark-brown ribbed cotton tank with scoop neckline, "
            "thin delicate gold chain at the neckline, slim gold "
            "bangle on one wrist"
        ),
        energy="polished confident talking-head creator energy",
    )

    brief = (
        "talking-head UGC content for a creator-education brand — "
        "a polished micro-influencer explaining something to her "
        "audience from her content corner"
    )

    print("=== Step A: writing character ref prompt + anchor prompt in parallel ===\n")
    char_ref_prompt, anchor_prompt = await asyncio.gather(
        gem.write_character_prompt(brief=brief, character=char),
        gem.write_first_frame_prompt(
            brief=brief, character=char,
            opening_chunk_action=(
                "creator mid-sentence with one hand gesturing softly, "
                "explaining something with calm confidence"
            ),
            framing="selfie",
            product_in_first_frame=False,
            product_ref_url=None,
        ),
    )
    print("--- character ref prompt (first 200 chars) ---")
    print(char_ref_prompt[:200] + "...\n")
    print("--- anchor prompt (first 200 chars) ---")
    print(anchor_prompt[:200] + "...\n")

    print("=== Step B: rendering character reference (NB Pro, hero) ===\n")
    char_ref_images = await seedr.generate_async(
        prompt=char_ref_prompt, tier="hero", source="tab3",
    )
    character_ref = char_ref_images[0]
    print(f"Character ref URL: {character_ref.url}\n")

    print("=== Step C: rendering anchor frame (NB2) WITH char ref as input ===\n")
    anchor_images = await seedr.generate_async(
        prompt=anchor_prompt, tier="anchor", source="tab3",
        reference_image_urls=[character_ref.url],
    )
    anchor = anchor_images[0]
    print(f"Anchor URL: {anchor.url}\n")

    # Download both locally for visual comparison.
    ts = time.strftime("%Y%m%d-%H%M%S")
    char_local = ROOT / "generated-images" / f"charref-{ts}.jpg"
    anchor_local = ROOT / "generated-images" / f"anchor-locked-{ts}.jpg"
    char_local.parent.mkdir(parents=True, exist_ok=True)

    urllib.request.urlretrieve(character_ref.url, char_local)
    urllib.request.urlretrieve(anchor.url, anchor_local)

    print(f"Saved character ref:     {char_local}")
    print(f"Saved identity-locked anchor: {anchor_local}")


if __name__ == "__main__":
    asyncio.run(main())
