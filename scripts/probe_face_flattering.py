"""Verify the skin fix: re-render the run's EXACT character with the new
flattering write_character_prompt and save it, so we can eyeball clean/beautiful
skin vs the dirty/blotchy original (jobs/workflows/v2-20260630-121447 char ref).
Only variable changed = the prompt's skin language (same character, same brief).
"""
import asyncio
import glob
import json
from pathlib import Path

from app.config import load_settings
from app.gemini import CharacterDescription, GeminiClient, VoicePersona
from app.image_gen import make_image_client
from app.stitcher import download
from app.uploader import make_uploader

RUN = Path("jobs/workflows/v2-20260630-121447")
OUT = Path("jobs/probe-face")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    root = Path(__file__).resolve().parent.parent
    gem = GeminiClient(
        s, image_skill_md_path=root / "docs/character-image-skill.md",
        skill_md_path=root / "video-seedance-skill.md",
        llm_provider="gemini",   # Anthropic credits exhausted; Gemini writes the prompt
    )
    image = make_image_client(s, make_uploader(s))

    m = json.load(open(glob.glob(str(RUN / "manifest*.json"))[0]))
    brief = m.get("brief") or m.get("params", {}).get("brief") or ""
    cdict = dict(m["character"])
    cdict.setdefault("voice_persona", VoicePersona(
        gender=cdict.get("gender", "female"), age_band="late-20s",
        timbre="warm clear", pace="brisk", warmth="confident").model_dump())
    character = CharacterDescription(**cdict)

    prompt = await gem.write_character_prompt(brief=brief, character=character)
    print("=== NEW char-ref prompt (flattering) ===")
    print(prompt[:1500])
    imgs = await image.generate_async(prompt=prompt, tier="hero", source="workflow")
    url = imgs[0].url
    out = OUT / "char_ref_flattering.jpg"
    download(url, out)
    print(f"\nSAVED → {out}\nurl: {url}")
    print("COMPARE skin to the original dirty char ref in the run dir.")


if __name__ == "__main__":
    asyncio.run(main())
