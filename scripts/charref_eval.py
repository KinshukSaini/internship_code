"""Char-ref repro / fix-eval harness.

Replicates the pipeline's `hero` tier image config (Nano Banana Pro, 2K, 9:16)
exactly, but writes bytes to a local folder instead of S3 so we can eyeball
the results. Also can re-derive a char-ref prompt from a brief+character via
the app's `write_character_prompt` (so we can A/B the skill MD).

Usage:
  uv run python scripts/charref_eval.py gen   <prompt_file> <out_prefix> <N>
  uv run python scripts/charref_eval.py prompt <manifest_json> <out_prompt_file>
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=False)

OUT_DIR = Path("charref-eval")
OUT_DIR.mkdir(exist_ok=True)
MODEL = "gemini-3-pro-image-preview"  # NB Pro / hero tier
_SIZE = ["1K"]


def _client():
    import os
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())


def _hero_config():
    # Mirror app/image_gen.py::_config_for_tier(tier="hero", size="2K", 9:16)
    from google.genai import types
    return types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio="9:16", image_size=_SIZE[0]),
    )


def _save(resp, out: Path) -> bool:
    import base64
    for cand in (resp.candidates or []):
        content = getattr(cand, "content", None)
        if content is None:
            continue
        for part in (content.parts or []):
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            raw = getattr(inline, "data", None)
            if not raw:
                continue
            if isinstance(raw, str):
                raw = base64.b64decode(raw)
            out.write_bytes(raw)
            return True
    return False


def gen(prompt_file: str, out_prefix: str, n: int, size: str = "1K"):
    prompt = Path(prompt_file).read_text()
    client = _client()
    _SIZE[0] = size
    cfg = _hero_config()
    print(f"[gen] {n}x  size={size}  prompt_words={len(prompt.split())}  prefix={out_prefix}")
    for i in range(1, n + 1):
        out = OUT_DIR / f"{out_prefix}-{i}.png"
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt, config=cfg,
            )
            ok = _save(resp, out)
            print(f"  [{i}/{n}] {'saved ' + str(out) if ok else 'NO IMAGE in response'}")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{n}] ERROR: {type(e).__name__}: {e}")


def write_prompt(manifest_json: str, out_prompt_file: str):
    """Re-derive a char-ref prompt from a manifest's brief+character using the
    CURRENT skill MD (so editing the MD and re-running shows the A/B)."""
    import json
    from app.config import load_settings  # noqa
    from app.gemini import GeminiClient
    from app.models import CharacterDescription

    ROOT = Path(__file__).resolve().parents[1]
    settings = load_settings()
    m = json.load(open(manifest_json))
    brief = m["brief"]
    char = CharacterDescription(**m["character"])
    gem = GeminiClient(
        settings=settings,
        image_skill_md_path=ROOT / "docs/character-image-skill.md",
        skill_md_path=ROOT / "video-seedance-skill.md",
    )
    prompt = asyncio.run(gem.write_character_prompt(brief=brief, character=char))
    Path(out_prompt_file).write_text(prompt)
    print(f"[prompt] wrote {len(prompt.split())} words -> {out_prompt_file}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "gen":
        gen(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5] if len(sys.argv)>5 else "1K")
    elif cmd == "prompt":
        write_prompt(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
