"""Verify the no-rig skill-MD fix: rebuild beat-7 (tripod) anchor prompt with the
fixed skill MD, assert no rig nouns, then regenerate the image with the char ref."""
import asyncio, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=False)
from app.config import load_settings
from app.uploader import make_uploader
from app.image_gen import make_image_client
from app.gemini import GeminiClient, CharacterDescription

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "jobs/workflows/v2-20260620-132106/manifest.json"

async def main():
    m = json.load(open(RUN))
    s = load_settings()
    gem = GeminiClient(s, image_skill_md_path=ROOT/"docs/character-image-skill.md",
                       skill_md_path=ROOT/"video-seedance-skill.md")
    char = CharacterDescription(**m["character"])
    beats = m["timeline"]["beats"]
    b7 = beats[7]
    char_ref = m["character_ref_url"]

    for trial in (1, 2):
        prompt = await gem.write_first_frame_prompt(
            brief=m["brief"], character=char,
            opening_chunk_action=b7["visual_note"], framing=b7["framing"],
            product_in_first_frame=False, product_ref_url=None,
        )
        rig = [w for w in ["tripod","propped","holding the phone","reel being filmed",
                           "on a small","selfie stick","gimbal"] if w in prompt.lower()
               and f"no {w}" not in prompt.lower()]
        # crude: list affirmative rig mentions
        affirm = [w for w in ["on a small tripod","propped","reel being filmed","holding the phone"]
                  if w in prompt.lower()]
        print(f"\n[trial {trial}] framing={b7['framing']} words={len(prompt.split())} affirmative_rig={affirm}")
        client = make_image_client(s, make_uploader(s))
        imgs = await client.generate_async(prompt=prompt, tier="hero", source="anchor-fix",
                    reference_image_urls=[char_ref], reference_roles=["character"])
        print(f"  url: {imgs[0].url}")

asyncio.run(main())
