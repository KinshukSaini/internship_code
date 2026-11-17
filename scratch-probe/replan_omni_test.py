"""Test the planner + pacing + multi-shot changes WITHOUT Gemini.

Gemini image credits are depleted (429), so a full omni run cannot render its
character ref or anchors. The planner runs on Claude, which IS funded — and the
planner is where the biggest change landed (_has_interior_phrase_boundary now
counts the comma, so the 8-word fast-cut cap actually fires). Everything after
it — _omni_windows, the multi-shot fold, the prompt builders, the product-ref
gate — is pure local code.

So: re-plan the SAME brief, then run the real downstream code over the result
and print what the run WOULD order. Costs a few cents of Claude, no video.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST = Path("/Users/harshkedia/Downloads/manifest.json")


def _load_env() -> None:
    import os
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    _load_env()
    from app.config import load_settings
    from app.gemini import (GeminiClient, CharacterDescription, _AUDIO_TAG_RE,
                            _MAX_BEAT_WORDS)
    from app.pipeline import (ROOT as PROOT, _beat_uses_product, _fallback_prompt,
                              _omni_duration, _omni_windows)

    data = json.loads(MANIFEST.read_text())
    brief = data["brief"]
    character = CharacterDescription.model_validate(data["character"])
    product_ref = data["params"]["product_ref_url"]

    gem = GeminiClient(
        load_settings(),
        image_skill_md_path=PROOT / "docs/character-image-skill.md",
        skill_md_path=PROOT / "video-seedance-skill.md",
        omni_skill_md_path=PROOT / "video-omni-skill.md",
        llm_provider="claude",
    )
    print("re-planning the same brief with the fixed word cap...\n")
    tl = await gem.plan_timeline(
        brief=brief, character=character, product_present=bool(product_ref),
        target_duration_s=data["params"].get("target_duration_s"),
        words_per_second=2.0,
        product_description=data.get("product_description", "") or "",
    )
    name = getattr(tl, "product_name", None)

    old_beats = data["beats"]
    old_over = sum(1 for b in old_beats
                   if len(_AUDIO_TAG_RE.sub("", b["dialogue_text"] or "").split())
                   > _MAX_BEAT_WORDS)
    new_over = sum(1 for b in tl.beats
                   if len(_AUDIO_TAG_RE.sub("", b.dialogue_text or "").split())
                   > _MAX_BEAT_WORDS)
    print(f"beats:            {len(old_beats)} -> {len(tl.beats)}")
    print(f"over the {_MAX_BEAT_WORDS}-word cap: {old_over} -> {new_over}")
    print(f"product_name:     {name!r}")
    print(f"product_in_frame flags set by the planner: "
          f"{sum(1 for b in tl.beats if getattr(b, 'product_in_frame', False))}"
          f"/{len(tl.beats)}\n")

    beats, wins, n_split = _omni_windows(
        tl.beats, product_ref_url=product_ref, product_name=name,
        on_progress=lambda m: print(f"  {m}"))

    print(f"\n{'clip':>4} {'shots':>5} {'take':>6} {'ord':>4} {'longest':>8} "
          f"{'prod':>5}  sections")
    total = longest = 0
    for i, (b, w) in enumerate(zip(beats, wins)):
        ln = w[1] - w[0]
        od = _omni_duration(ln)
        total += od
        shots = list(getattr(b, "shots", None) or [b])
        lens = list(getattr(b, "shot_lens", None) or [ln])
        longest = max(longest, max(lens))
        prod = any(_beat_uses_product(s, product_ref, name) for s in shots)
        print(f"{i:>4} {len(shots):>5} {ln:>5.2f}s {od:>3}s {max(lens):>7.2f}s "
              f"{('YES' if prod else '-'):>5}  "
              f"{[s.ad_section[:4] for s in shots]}")
    print(f"\nclips {len(beats)} | ordered {total}s (~${total * 0.087:.2f}) "
          f"| LONGEST SHOT {longest:.2f}s")
    print(f"beats with a product reference attached: "
          f"{sum(1 for b in beats for s in (getattr(b,'shots',None) or [b]) if _beat_uses_product(s, product_ref, name))}")

    # One real multi-shot prompt, as the run would send it.
    grp = next((i for i, b in enumerate(beats)
                if len(getattr(b, "shots", None) or []) > 1), None)
    if grp is not None:
        w = wins[grp]
        print("\n" + "=" * 72 + f"\nPROMPT for clip {grp}\n" + "=" * 72)
        print(_fallback_prompt(beats[grp], w[1] - w[0], lipsync_engine="syncso",
                               omni_mode=True, persona_clause="[persona]"))
    (ROOT / "scratch-probe/replanned-timeline.json").write_text(
        json.dumps(tl.model_dump(), indent=1))
    print("\ntimeline → scratch-probe/replanned-timeline.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
