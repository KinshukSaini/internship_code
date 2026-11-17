# Opus-written Seedance Motion Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Git note:** This repo's rule is *commit only when the user asks*. Commit steps are included per plan structure; run them only with the user's go-ahead. Current branch is `harsh/add-make-new-changes` (not `main`), so no new branch is needed.

**Goal:** Replace v2's hardcoded f-string Seedance motion prompts with per-beat prompts written by Claude Opus — guided by `video-seedance-skill.md`, grounded in the rendered anchor image, timeline-aware, generated concurrently, persisted to the manifest for debugging, with the f-strings kept as a graceful fallback.

**Architecture:** A new concurrent pipeline stage (`_write_motion_prompts`, "stage 5b") runs after anchors and before clips. For each non-reaction beat under the `claude` provider it calls a new `GeminiClient.write_motion_prompt`, which sends the skill MD + full-timeline overview as the cached system instruction and the anchor frame as a multimodal image, returning a `MotionPrompt{prompt, restaged}`. On any failure (or non-claude provider / reaction beat) that beat uses its existing f-string. Results are persisted debug-rich to `manifest["motion_prompts"]`, and `_clip` consumes them instead of building the prompt inline.

**Tech Stack:** Python 3.11/3.12, asyncio, Anthropic Python SDK (`messages.create`/`messages.parse`), Pydantic v2, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-16-opus-seedance-prompts-design.md`
**Realism sources:** `docs/seedance-realism-guide.md`, `docs/seedance-prompting-guide.md`, `docs/naturalness-audit-2026-06.md`

---

### Task A: Revise `video-seedance-skill.md` into the v2 craft bible

`video-seedance-skill.md` is the system instruction Opus reads in Task 3, so it must
be the single source of truth for "how to write a good Seedance i2v prompt." Today it
has two defects: (1) the entire body is **duplicated** (pasted twice — once escaped,
once clean), and (2) it predates the realism findings and is shaped for the old v1
"≤350 word / 5-section" format rather than the tight per-beat i2v prompt v2 needs.

**Files:**
- Modify: `video-seedance-skill.md`
- (No test — content doc; the existing `_load_skill_md` test still applies.)

**This is a judgment/content task (no code), executed by an Opus subagent.** Acceptance
criteria — the revised skill MUST:

- [ ] **De-duplicate.** One clean copy of the content; remove the escaped duplicate.
- [ ] **Keep the genuinely-good craft rules:** hard-separate CAMERA and SUBJECT into
  two sentences; camera-stillness vocabulary; dialogue MUST be in straight double
  quotes (for the r2v/native path); asset-reference syntax ("the woman from Image 1
  (the creator)"); the Hinglish/Indian-English voice-biography audio direction (keep
  whole — it's strong); the anti-glitch motion guidance.
- [ ] **Fold in the realism digest (reconciled from the three realism docs):**
  - Skin micro-texture — **flattering, not blotchy**: "clear, healthy, attractive skin
    with fine natural texture and subtle pores," NOT "visible pores / vellus hair /
    unretouched" (the latter makes good-looking creators read greasy/haggard — see
    `seedance-realism-guide.md` §3d calibration note).
  - First-frame faithfulness clause: "preserve the composition and colors of the
    reference image" (anti-morph/anti-drift). Match lighting, don't impose it.
  - Lighting: prefer "even, soft, diffuse light with steady/constant exposure" over
    "soft daylight" (kills skin shimmer-in-motion).
  - Constraints are **affirmative/inline only** (Seedance has NO negative-prompt
    field). The ONE safe negation is `no 3D, no cartoon, no VFX`. NEVER negate a
    subject noun ("no plastic skin" backfires and can introduce the artifact).
  - Product/hands b-roll: locked-off tripod phrasing (the `camera_fixed` boolean is
    not reliably honored — the in-prompt lock is the real lever); "realistic material
    reflections," **no film grain** (grain dirties the label).
  - "Describe only motion, protect the still" — do NOT re-describe subject/scene/
    wardrobe (the anchor carries them); name only what changes + what to preserve.
- [ ] **Reconcile the length/format guidance for i2v:** replace the v1 "cap at 350
  words / 5-section" framing with the i2v reality — a single tight prompt, ~60–100
  words effective, **front-loaded** (Seedance down-weights trailing tokens), ordered
  motion → camera → texture/optics → faithfulness → short constraint tail. Keep it
  flexible (not a hard truncation), but make clear that longer ≠ better.
- [ ] **Replace the worked example** with one that matches the new tight i2v shape
  (motion + camera + texture + faithfulness, grounded in an anchor — no scene
  re-description), instead of the old 350-word 5-section example.
- [ ] **Do NOT break the v1 path:** keep the craft universal; the legacy
  `write_seedance_prompt` still loads this file. The realism/camera/audio rules
  improve v1 too; only the format guidance is retuned toward v2.
- [ ] Verify the file still loads: `python -c "from pathlib import Path; assert Path('video-seedance-skill.md').read_text().strip()"`.

This task is INDEPENDENT of the code tasks (different file) and can run in parallel
with Task 3's gemini work.

---

### Task 1: Multimodal `image_url` on `ClaudeBackend.generate`

Add an optional `image_url` so a call can attach an image. The anchor's hosted URL is the same one already handed to Seedance as an image input — known-public — so we use a `url` image source (no download). Text-only callers are unchanged.

**Files:**
- Modify: `app/claude.py:33-87` (`ClaudeBackend.generate`)
- Test: `tests/test_claude.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claude.py` (the `_fake_anthropic`, `_text_resp` helpers already exist at the top of the file):

```python
@pytest.mark.asyncio
async def test_generate_attaches_image_url_as_multimodal_content():
    fake = _fake_anthropic(create_resp=_text_resp("a prompt"))
    backend = ClaudeBackend(_settings(), _client=fake)
    out = await backend.generate(
        tier="fast", prompt="describe the motion",
        image_url="https://host/anchor-3.png",
    )
    assert out == "a prompt"
    msgs = fake.messages.create.call_args.kwargs["messages"]
    assert msgs == [{
        "role": "user",
        "content": [
            {"type": "image",
             "source": {"type": "url", "url": "https://host/anchor-3.png"}},
            {"type": "text", "text": "describe the motion"},
        ],
    }]


@pytest.mark.asyncio
async def test_generate_without_image_url_keeps_string_content():
    fake = _fake_anthropic(create_resp=_text_resp("x"))
    backend = ClaudeBackend(_settings(), _client=fake)
    await backend.generate(tier="fast", prompt="hello")
    msgs = fake.messages.create.call_args.kwargs["messages"]
    assert msgs == [{"role": "user", "content": "hello"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claude.py::test_generate_attaches_image_url_as_multimodal_content -v`
Expected: FAIL — `TypeError: generate() got an unexpected keyword argument 'image_url'`.

- [ ] **Step 3: Implement**

In `app/claude.py`, change the `generate` signature and build `content` before `kwargs`:

```python
    async def generate(self, *, tier: str, prompt: str, schema=None,
                       system_instruction: str | None = None,
                       temperature: float = 0.4,
                       image_url: str | None = None) -> Any:
        """One call against the tier's Claude model.

        tier "pro" → Opus: adaptive thinking + effort high; temperature is
        REMOVED on Opus (400 if sent) so it is never forwarded. tier "fast"
        → Sonnet: caller temperature, effort medium (its default is high).
        tier "lite" → Haiku: caller temperature, no output_config (effort
        unsupported). With schema: messages.parse → validated instance.
        Without: messages.create → text string.

        image_url: when set (non-empty), the user turn becomes multimodal
        (image + text) via a `url` image source — used for anchor-grounded
        motion prompts. An empty string degrades to a text-only turn.
        """
        if image_url:
            content: Any = [
                {"type": "image",
                 "source": {"type": "url", "url": image_url}},
                {"type": "text", "text": prompt},
            ]
        else:
            content = prompt
        kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": content}],
        }
```

Leave the rest of the method (tier branches, system cache_control, schema parse/create, logging, refusal) exactly as-is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claude.py -v`
Expected: PASS (both new tests + all existing).

- [ ] **Step 5: Commit**

```bash
git add app/claude.py tests/test_claude.py
git commit -m "feat(claude): optional image_url for multimodal generate()"
```

---

### Task 2: Thread `image_url` through `GeminiClient._generate`

The seam every text task routes through gains an optional `image_url`, forwarded to the Claude backend. The Gemini branch ignores it (this feature runs under the `claude` provider; the f-string fallback covers the gemini case).

**Files:**
- Modify: `app/gemini.py:791-821` (`_generate`)
- Test: `tests/test_gemini.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gemini.py` (it already imports `GeminiClient`; its `_settings()` helper defaults to `llm_provider="gemini"` and takes no overrides, so this test builds a claude-configured `Settings` inline):

```python
@pytest.mark.asyncio
async def test_generate_forwards_image_url_to_claude_backend():
    from unittest.mock import AsyncMock
    from app.config import Settings
    s = Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="",
        elevenlabs_api_key="el", sync_api_key="sy",
        anthropic_api_key="ant-test", llm_provider="claude",
    )
    c = GeminiClient(s, _claude_client=object())   # provider=claude
    c._claude = AsyncMock()
    c._claude.generate = AsyncMock(return_value="ok")
    out = await c._generate(
        tier="pro", prompt="p", system_instruction="sys",
        image_url="https://host/a.png",
    )
    assert out == "ok"
    assert c._claude.generate.call_args.kwargs["image_url"] == \
        "https://host/a.png"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gemini.py::test_generate_forwards_image_url_to_claude_backend -v`
Expected: FAIL — `_generate() got an unexpected keyword argument 'image_url'`.

- [ ] **Step 3: Implement**

In `app/gemini.py`, update `_generate`'s signature and the claude branch only:

```python
    async def _generate(self, *, tier: str, prompt: str, schema=None,
                        system_instruction: str | None = None,
                        temperature: float = 0.4,
                        image_url: str | None = None) -> Any:
        """Provider seam for every text task.

        tier: "pro" (creative core), "fast" (workhorse), "lite" (cheap
        binary decisions — rides the fast model on Gemini, which has no
        cheaper text tier). With a schema, returns the validated Pydantic
        object; without one, returns the raw text.

        image_url: claude-only multimodal grounding (anchor frame). The
        gemini branch ignores it — the motion-prompt feature runs under the
        claude provider, with an f-string fallback otherwise.
        """
        if self._provider == "claude":
            return await self._claude.generate(
                tier=tier, prompt=prompt, schema=schema,
                system_instruction=system_instruction,
                temperature=temperature, image_url=image_url,
            )
```

Leave the gemini branch (model select, `generate_content`, schema validation) below it unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gemini.py -v -k "generate_forwards_image_url or write_seedance or write_first_frame"`
Expected: PASS (new test + existing `_generate` consumers unaffected).

- [ ] **Step 5: Commit**

```bash
git add app/gemini.py tests/test_gemini.py
git commit -m "feat(gemini): thread image_url through _generate to claude backend"
```

---

### Task 3: `MotionPrompt` schema + `GeminiClient.write_motion_prompt`

The Opus call. Skill MD + timeline overview = cached system instruction; anchor frame = multimodal image; per-beat + mode-specific user message. Returns a validated `MotionPrompt{prompt, restaged}` (the `restaged` flag is the debug signal for "did Opus deviate from the planner"). No `temperature` — Opus drops it.

**Files:**
- Modify: `app/gemini.py` (add `MotionPrompt` near the other Pydantic models, e.g. after `TimelineBeat` ~line 277; add `write_motion_prompt` after `write_seedance_prompt` ~line 1996)
- Test: `tests/test_gemini.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gemini.py`:

```python
@pytest.mark.asyncio
async def test_write_motion_prompt_sync_talk_uses_skill_image_and_no_line(tmp_path):
    from unittest.mock import AsyncMock
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL RULES")
    c = GeminiClient(_settings(), _client=object(),
                     skill_md_path=skill_path)   # _generate is mocked below,
    # so the provider is irrelevant; _client=object() avoids building genai.
    c._generate = AsyncMock(
        return_value=MotionPrompt(prompt="tight prompt", restaged=False))

    beat = SimpleNamespace(
        index=2, ad_section="solution", shot_type="talking_head",
        framing="selfie", broll_type=None, broll_shot=None,
        dialogue_text="this changed my mornings", visual_note="she grins",
        beat_kind="speech")
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="long_loose_natural_waves",
        setting="apartment_kitchen_counter", expression="candid_mid_laugh",
        hair="wavy", face="smooth", wardrobe="tee", energy="warm",
        voice_vibe="upbeat_lively")

    out = await c.write_motion_prompt(
        beat=beat, character=char, window_len=3.5, mode="sync_talk",
        uses_product=False, anchor_url="https://host/a2.png",
        timeline_overview="beat 2 [solution/talking_head/speech]: ...",
        fallback_example="BASELINE FSTRING")
    assert out.prompt == "tight prompt" and out.restaged is False

    kw = c._generate.call_args.kwargs
    assert kw["tier"] == "pro"
    assert kw["schema"] is MotionPrompt
    assert kw["image_url"] == "https://host/a2.png"
    assert kw["system_instruction"].startswith("SKILL RULES")
    assert "beat 2 [solution" in kw["system_instruction"]   # timeline folded in
    # sync_talk must NOT put the spoken line in the prompt request
    assert "this changed my mornings" not in kw["prompt"]
    assert "BASELINE FSTRING" in kw["prompt"]               # baseline handed as ref
    assert "LOCKED" in kw["prompt"]                          # anchor-lock instruction


@pytest.mark.asyncio
async def test_write_motion_prompt_native_talk_quotes_the_line(tmp_path):
    from unittest.mock import AsyncMock
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL")
    c = GeminiClient(_settings(), _client=object(),
                     skill_md_path=skill_path)   # _generate is mocked below,
    # so the provider is irrelevant; _client=object() avoids building genai.
    c._generate = AsyncMock(
        return_value=MotionPrompt(prompt="p", restaged=True))
    beat = SimpleNamespace(
        index=0, ad_section="hook", shot_type="talking_head", framing="lav_mic",
        broll_type=None, broll_shot=None, dialogue_text="okay so listen",
        visual_note="she leans in", beat_kind="speech")
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="long_loose_natural_waves",
        setting="kitchen", expression="candid", hair="wavy", face="smooth",
        wardrobe="tee", energy="warm", voice_vibe="upbeat_lively")
    await c.write_motion_prompt(
        beat=beat, character=char, window_len=4.0, mode="native_talk",
        uses_product=False, anchor_url="https://host/a0.png",
        timeline_overview="ov", fallback_example="BASE")
    kw = c._generate.call_args.kwargs
    assert '"okay so listen"' in kw["prompt"]   # line in quotes
    assert "Audio 1" in kw["prompt"]            # audio binding
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gemini.py -v -k write_motion_prompt`
Expected: FAIL — `ImportError: cannot import name 'MotionPrompt'` (and `AttributeError: write_motion_prompt`).

- [ ] **Step 3: Implement the schema**

In `app/gemini.py`, after the `TimelineBeat` model (~line 277), add:

```python
class MotionPrompt(BaseModel):
    prompt: str = Field(
        description="The final Seedance video prompt for THIS single beat, "
                    "plain text. Camera and subject in separate sentences. "
                    "Resourceful and specific — every clause does work; no "
                    "filler, and never restate what the anchor image already "
                    "shows. No length cap.",
    )
    restaged: bool = Field(
        description="True ONLY if you changed the planner's staged action / "
                    "scene for this beat; False if you kept the planner's "
                    "visual_note intent.",
    )
```

- [ ] **Step 4: Implement the method**

In `app/gemini.py`, after `write_seedance_prompt` (~line 1996), add the mode-task table and method:

```python
_MOTION_MODE_TASK = {
    "sync_talk": (
        "MODE: talking-head, lipsynced by a downstream pass. Direct continuous "
        "talking motion — mouth clearly opening and closing through the WHOLE "
        "clip, natural blinks — and any gesture must BEGIN and RESOLVE within "
        "{window_len:.1f}s. Do NOT write the spoken line; the mouth is corrected "
        "downstream."
    ),
    "native_talk": (
        "MODE: talking-head, audio-driven. Put the spoken line in double quotes "
        "and state that her voice and lip movements follow Audio 1. The reference "
        "audio paces the take — no gesture-window budget. Line: \"{line}\"."
    ),
    "broll": (
        "MODE: b-roll cutaway (voiceover continues underneath). Direct ONE "
        "subject action and ONE camera move. Product/hands shots use a static, "
        "locked-off camera — a moving camera over a product is the #1 morph "
        "source."
    ),
}


    async def write_motion_prompt(
        self, *, beat: Any, character: CharacterDescription,
        window_len: float, mode: str, uses_product: bool,
        anchor_url: str, timeline_overview: str,
        fallback_example: str,
    ) -> "MotionPrompt":
        """Opus writes ONE beat's Seedance prompt, grounded in its anchor frame.

        system = skill MD + full-timeline overview (cached prefix shared across
        the per-beat fan-out). The anchor frame rides as a multimodal image.
        Returns a validated MotionPrompt; `restaged` records whether Opus chose
        to deviate from the planner's visual_note. No temperature — Opus drops
        it on the pro tier.
        """
        skill_md = self._load_skill_md()
        system = (
            f"{skill_md}\n\n"
            f"=== TIMELINE CONTEXT (every beat in this video — keep motion and "
            f"energy flowing across cuts) ===\n{timeline_overview}"
        )
        line = (beat.dialogue_text or "").strip()
        task = _MOTION_MODE_TASK[mode].format(window_len=window_len, line=line)
        char_block = (
            f"Creator (LOCKED — the same person in every beat): {character.age} "
            f"{character.gender}, {character.ethnicity}; hair {character.hair}; "
            f"face {character.face}; wardrobe {character.wardrobe}; setting "
            f"{character.setting}; energy {character.energy}."
        )
        framing = f"/{beat.framing}" if beat.framing else ""
        broll = f", broll {beat.broll_type}" if beat.broll_type else ""
        product = "; product is in frame" if uses_product else ""
        user_prompt = (
            "Write the Seedance video prompt for ONE beat, applying the craft "
            "rules in the system instruction.\n\n"
            "Image 1 is the ALREADY-RENDERED opening frame for THIS beat. The "
            "scene, setting, wardrobe, props and creator identity are LOCKED by "
            "it. Do NOT re-describe them — direct only MOTION, CAMERA and AUDIO. "
            "Re-describing what is already in frame makes the model morph it.\n\n"
            f"{char_block}\n\n"
            f"THIS beat #{beat.index} — section {beat.ad_section}, shot "
            f"{beat.shot_type}{framing}{broll}{product}. Clip is "
            f"{window_len:.1f}s.\n"
            f"Planner's staging (visual_note): {beat.visual_note}\n"
            "Honor this staging by DEFAULT. Re-stage or change the action ONLY "
            "if Image 1 contradicts it or the planned action would look "
            "unnatural / glitchy — otherwise keep the planner's intent. Set "
            "'restaged' accordingly.\n\n"
            f"{task}\n\n"
            "Be resourceful and dense — every clause earns its place. Apply the "
            "realism + camera/subject + anti-CGI rules from the system "
            "instruction (you decide how to fold them in). Order the prompt the "
            "way Seedance weights it: motion -> camera -> texture/faithfulness "
            "-> a short constraint tail. Seedance down-weights trailing tokens, "
            "so front-load the highest-value direction and do not pad (~60-100 "
            "words is where it is most effective).\n\n"
            "A baseline prompt for this beat type is below — BEAT it, do not "
            f"copy it:\n---\n{fallback_example}\n---"
        )
        async def _do():
            return await self._generate(
                tier="pro", prompt=user_prompt, schema=MotionPrompt,
                system_instruction=system, image_url=anchor_url,
            )
        return await self._with_retry(_do, label="write_motion_prompt")
```

Note: `_MOTION_MODE_TASK` is a module-level dict — place it above the `GeminiClient` class (or at module top with the other constants). The method body sits inside the class.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_gemini.py -v -k write_motion_prompt`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add app/gemini.py tests/test_gemini.py
git commit -m "feat(gemini): write_motion_prompt — Opus writes per-beat Seedance prompts"
```

---

### Task 4: Pipeline pure helpers — mode, fallback, timeline overview

Three small pure functions stage 5b builds on. `_beat_mode` classifies a beat exactly as `_clip` does today; `_fallback_prompt` picks the matching f-string; `_timeline_overview` renders the cross-cut context.

**Files:**
- Modify: `app/pipeline.py` (add helpers near the other `_`-prefixed helpers, e.g. after `_broll_motion_prompt` ~line 508)
- Test: `tests/test_pipeline.py` (the `_tbeat()` factory already exists ~line 99)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline.py`:

```python
def test_beat_mode_classifies_every_beat_kind():
    from app.pipeline import _beat_mode
    # native talking-head only when the engine is seedance_native
    assert _beat_mode(_tbeat(shot_type="talking_head"),
                      lipsync_engine="seedance_native") == "native_talk"
    assert _beat_mode(_tbeat(shot_type="talking_head"),
                      lipsync_engine="syncso") == "sync_talk"
    assert _beat_mode(_tbeat(shot_type="broll", framing=None),
                      lipsync_engine="seedance_native") == "broll"
    # a reaction beat is never audio-driven, regardless of engine
    assert _beat_mode(_tbeat(shot_type="talking_head", beat_kind="reaction"),
                      lipsync_engine="seedance_native") == "reaction"


def test_fallback_prompt_matches_the_clip_path():
    from app.pipeline import (_fallback_prompt, _talking_motion_prompt,
                              _broll_motion_prompt)
    b = _tbeat(shot_type="talking_head")
    assert _fallback_prompt(b, 4.0, lipsync_engine="syncso") == \
        _talking_motion_prompt(b, 4.0)
    bb = _tbeat(shot_type="broll", framing=None, broll_type="lifestyle",
                broll_shot="a shot", visual_note="she pours")
    assert _fallback_prompt(bb, 4.0, lipsync_engine="syncso") == \
        _broll_motion_prompt("a shot", "she pours", "lifestyle")


def test_timeline_overview_lists_every_beat_with_section_and_line():
    from app.pipeline import _timeline_overview
    beats = [_tbeat(index=0, ad_section="hook", dialogue_text="okay so"),
             _tbeat(index=1, ad_section="cta", shot_type="broll",
                    framing=None, dialogue_text="")]
    ov = _timeline_overview(beats)
    assert "beat 0" in ov and "hook" in ov and "okay so" in ov
    assert "beat 1" in ov and "cta" in ov
    assert "(silent)" in ov          # empty dialogue rendered explicitly
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pipeline.py -v -k "beat_mode or fallback_prompt or timeline_overview"`
Expected: FAIL — `ImportError: cannot import name '_beat_mode'`.

- [ ] **Step 3: Implement**

In `app/pipeline.py`, after `_broll_motion_prompt` (~line 508), add:

```python
def _beat_mode(beat: Any, *, lipsync_engine: str) -> str:
    """Classify a beat for prompt generation — mirrors _clip's branch logic.

    reaction → silent hold (skips Opus); native_talk → audio-driven r2v;
    sync_talk → talking head lipsynced downstream; broll → cutaway.
    """
    is_reaction = getattr(beat, "beat_kind", "speech") == "reaction"
    if is_reaction:
        return "reaction"
    if (lipsync_engine == "seedance_native"
            and beat.shot_type == "talking_head"):
        return "native_talk"
    if beat.shot_type == "talking_head":
        return "sync_talk"
    return "broll"


def _fallback_prompt(beat: Any, window_len: float, *,
                     lipsync_engine: str) -> str:
    """The deterministic f-string for a beat — Opus's fallback AND the
    baseline example handed to Opus as the bar to beat."""
    mode = _beat_mode(beat, lipsync_engine=lipsync_engine)
    if mode == "native_talk":
        return _native_talking_prompt(beat)
    if mode == "sync_talk":
        return _talking_motion_prompt(beat, window_len)
    # broll AND reaction both go through the silent broll prompt today
    return _broll_motion_prompt(
        beat.broll_shot or "", beat.visual_note,
        getattr(beat, "broll_type", None),
    )


def _timeline_overview(beats: list) -> str:
    """Compact full-timeline context shared across the per-beat fan-out, so
    Opus keeps motion/energy flowing across cuts instead of writing each beat
    in isolation."""
    lines = [
        "Full beat sequence (for cross-cut continuity — do NOT re-describe "
        "scenes, just be aware of the arc):"
    ]
    for b in beats:
        kind = getattr(b, "beat_kind", "speech")
        line = (b.dialogue_text or "").strip() or "(silent)"
        lines.append(
            f"  beat {b.index} [{b.ad_section}/{b.shot_type}/{kind}]: "
            f"{line[:80]}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pipeline.py -v -k "beat_mode or fallback_prompt or timeline_overview"`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add app/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): beat-mode / fallback / timeline-overview helpers"
```

---

### Task 5: `_write_motion_prompts` stage function

The concurrent stage. Per beat: pick mode + fallback; under the `claude` provider call Opus (catching any failure → fallback); record a debug-rich entry; persist to the manifest as each lands. Non-claude provider and reaction beats skip Opus. Extracted as a standalone function so it's testable with a fake `gem` (no real `generate_ugc_video`).

**Files:**
- Modify: `app/pipeline.py` (add `CLAUDE_PRO_MODEL` to the `from app.config import (...)` block ~line 40; add `_write_motion_prompts` after the helpers from Task 4)
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline.py`:

```python
class _FakeManifest:
    def __init__(self):
        self.data = {}
    def update(self, **fields):
        self.data.update(fields)


def _anchor(url):
    return SimpleNamespace(url=url)


@pytest.mark.asyncio
async def test_write_motion_prompts_opus_path_and_records(monkeypatch):
    from app.pipeline import _write_motion_prompts
    from app.gemini import MotionPrompt

    calls = []
    async def fake_write(**kw):
        calls.append(kw)
        return MotionPrompt(prompt=f"OPUS-{kw['beat'].index}", restaged=True)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    beats = [_tbeat(index=0, shot_type="talking_head"),
             _tbeat(index=1, shot_type="broll", framing=None,
                    broll_type="lifestyle", broll_shot="a shot")]
    anchors = [_anchor("u0"), _anchor("u1")]
    windows = [(0.0, 3.0), (3.0, 6.0)]
    man = _FakeManifest()

    recs = await _write_motion_prompts(
        beats=beats, anchors=anchors, windows=windows, gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=man, on_asset=None,
    )
    assert recs[0]["source"] == "opus" and recs[0]["prompt"] == "OPUS-0"
    assert recs[0]["restaged"] is True and recs[0]["mode"] == "sync_talk"
    assert recs[1]["mode"] == "broll" and recs[1]["source"] == "opus"
    assert man.data["motion_prompts"][0]["index"] == 0       # persisted
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_write_motion_prompts_falls_back_on_opus_error():
    from app.pipeline import _write_motion_prompts, _talking_motion_prompt
    async def boom(**kw):
        raise RuntimeError("opus down")
    gem = SimpleNamespace(write_motion_prompt=boom)
    beats = [_tbeat(index=0, shot_type="talking_head")]
    recs = await _write_motion_prompts(
        beats=beats, anchors=[_anchor("u0")], windows=[(0.0, 4.0)], gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert recs[0]["source"] == "fallback"
    assert recs[0]["prompt"] == _talking_motion_prompt(beats[0], 4.0)


@pytest.mark.asyncio
async def test_write_motion_prompts_skips_opus_for_reaction_and_non_claude():
    from app.pipeline import _write_motion_prompts
    called = {"n": 0}
    async def fake_write(**kw):
        called["n"] += 1
        from app.gemini import MotionPrompt
        return MotionPrompt(prompt="x", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    # reaction beat under claude → skipped
    rec = await _write_motion_prompts(
        beats=[_tbeat(index=0, shot_type="talking_head", beat_kind="reaction",
                      dialogue_text="")],
        anchors=[_anchor("u")], windows=[(0.0, 1.0)], gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert rec[0]["source"] == "reaction" and called["n"] == 0

    # non-claude provider → all fallback, gem never called
    rec2 = await _write_motion_prompts(
        beats=[_tbeat(index=0, shot_type="talking_head")],
        anchors=[_anchor("u")], windows=[(0.0, 4.0)], gem=gem,
        character=object(), llm="gemini", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert rec2[0]["source"] == "fallback" and called["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pipeline.py -v -k write_motion_prompts`
Expected: FAIL — `ImportError: cannot import name '_write_motion_prompts'`.

- [ ] **Step 3: Add the config import**

In `app/pipeline.py`, add `CLAUDE_PRO_MODEL` to the existing `from app.config import (` block (~line 40), keeping alphabetical/existing order, e.g.:

```python
from app.config import (
    CLAUDE_PRO_MODEL,
    # ...existing names...
)
```

- [ ] **Step 4: Implement the stage function**

In `app/pipeline.py`, after the Task 4 helpers, add:

```python
async def _write_motion_prompts(
    *, beats: list, anchors: list, windows: list, gem: Any,
    character: Any, llm: str, product_ref_url: str | None,
    lipsync_engine: str, on_progress: Callable[[str], None],
    manifest: Any, on_asset: Callable[[str, Any], None] | None,
) -> dict[int, dict]:
    """Stage 5b: per-beat Seedance motion prompts, written by Opus.

    Concurrent. Under the claude provider every non-reaction beat gets an
    Opus prompt grounded in its anchor frame; any failure (or a non-claude
    provider / reaction beat) falls back to the deterministic f-string so a
    paid run never dies on one bad sample. Each record is persisted debug-rich
    to manifest["motion_prompts"] as it lands.
    """
    _assert_aligned(beats=beats, anchors=anchors, windows=windows)
    overview = _timeline_overview(beats)
    records: dict[int, dict] = {}

    async def _one(i: int, beat: Any, anchor: Any,
                   window: tuple[float, float]) -> None:
        window_len = window[1] - window[0]
        mode = _beat_mode(beat, lipsync_engine=lipsync_engine)
        fallback = _fallback_prompt(beat, window_len,
                                    lipsync_engine=lipsync_engine)
        prompt, restaged, model = fallback, False, None
        if mode == "reaction":
            source = "reaction"
        elif llm != "claude":
            source = "fallback"
        else:
            try:
                mp = await gem.write_motion_prompt(
                    beat=beat, character=character, window_len=window_len,
                    mode=mode,
                    uses_product=_beat_uses_product(beat, product_ref_url),
                    anchor_url=anchor.url, timeline_overview=overview,
                    fallback_example=fallback,
                )
                prompt, restaged, source, model = (
                    mp.prompt, mp.restaged, "opus", CLAUDE_PRO_MODEL)
            except Exception as e:  # noqa: BLE001 — one beat never aborts the run
                on_progress(
                    f"  beat {i} motion prompt fell back to f-string: {e!r}")
                source = "fallback"
        records[i] = {
            "index": i, "mode": mode, "source": source, "prompt": prompt,
            "anchor_url": anchor.url, "planner_visual_note": beat.visual_note,
            "restaged": restaged, "model": model,
        }
        # asyncio is single-threaded — safe to mutate + persist here.
        manifest.update(
            motion_prompts=[records[k] for k in sorted(records)])

    await asyncio.gather(
        *[_one(i, b, a, w)
          for i, (b, a, w) in enumerate(zip(beats, anchors, windows))]
    )
    if on_asset:
        on_asset("motion_prompts", [records[k] for k in sorted(records)])
    return records
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_pipeline.py -v -k write_motion_prompts`
Expected: PASS (all three).

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): _write_motion_prompts stage (Opus + fallback + manifest)"
```

---

### Task 6: Wire stage 5b into `generate_ugc_video` and consume in `_clip`

Insert the stage call after anchors, and switch `_clip` to read the generated prompt instead of building it inline. The `native` audio setup stays — only the prompt assignment changes.

**Files:**
- Modify: `app/pipeline.py:874-877` (insert stage 5b after anchors persist), `app/pipeline.py:921-928` (replace inline prompt build in `_clip`)
- Test: full suite (the new behavior is covered by Tasks 3–5; this task verifies the wiring compiles and nothing regresses)

- [ ] **Step 1: Insert the stage call after anchors**

In `app/pipeline.py`, immediately after the anchors `on_asset("anchors", anchor_records)` block (~line 877) and before the `# 6. Per-beat clips` comment (~line 879), add:

```python
    # 5b. Per-beat Seedance motion prompts (Opus, parallel, multimodal) ---
    on_progress("writing per-beat Seedance motion prompts (Opus)...")
    motion_records = await _write_motion_prompts(
        beats=beats, anchors=anchors, windows=windows, gem=gem,
        character=character, llm=llm, product_ref_url=product_ref_url,
        lipsync_engine=lipsync_engine, on_progress=on_progress,
        manifest=manifest, on_asset=on_asset,
    )
```

- [ ] **Step 2: Consume the prompt in `_clip`**

In `app/pipeline.py`, replace the inline prompt build inside `_clip` — the block currently reading (lines ~921-928):

```python
            audio_url = uploader.upload(str(ref_padded))
            prompt = _native_talking_prompt(beat)
        elif beat.shot_type == "talking_head" and not is_reaction:
            prompt = _talking_motion_prompt(beat, window_len)
        else:
            prompt = _broll_motion_prompt(
                beat.broll_shot or "", beat.visual_note,
                getattr(beat, "broll_type", None),
            )
```

with (the `native` audio upload line stays; only the prompt selection changes):

```python
            audio_url = uploader.upload(str(ref_padded))
        # Prompt was written in stage 5b (Opus, or f-string fallback) and
        # persisted to the manifest; _clip just consumes it.
        prompt = motion_records[i]["prompt"]
```

- [ ] **Step 3: Verify the module imports and the full suite is green**

Run:
```bash
python -c "import app.pipeline, app.gemini, app.claude"
python -m pytest tests/test_pipeline.py tests/test_gemini.py tests/test_claude.py -q
```
Expected: import succeeds with no error; all three test files PASS.

- [ ] **Step 4: Confirm the wiring by inspection**

Run: `grep -n "motion_records\[i\]\|_write_motion_prompts\|_native_talking_prompt\|_talking_motion_prompt" app/pipeline.py`
Expected: `_clip` references `motion_records[i]["prompt"]`; `_write_motion_prompts` is called once in `generate_ugc_video`; the f-string builders are still referenced (inside `_fallback_prompt`), not dead.

- [ ] **Step 5: Run the whole test suite**

Run: `python -m pytest -q`
Expected: PASS (no regressions across the repo).

- [ ] **Step 6: Commit**

```bash
git add app/pipeline.py
git commit -m "feat(pipeline): wire Opus motion-prompt stage into generate_ugc_video"
```

---

## Self-Review

**Spec coverage:**
- Opus writes per-beat prompts, skill as system, Opus model → Task 3. ✓
- Multimodal anchor grounding → Tasks 1, 2 (plumbing), 3 (use). ✓
- Concurrent generation → Task 5 (`asyncio.gather`). ✓
- Timeline-aware (cached shared prefix) → Task 3 (folded into system) + Task 4 (`_timeline_overview`). ✓
- Staging authority (honor by default, restage when required, `restaged` flag) → Task 3 (user message + schema). ✓
- "Tight = resourceful, no word cap" → Task 3 (output instruction + schema description). ✓
- Graceful fallback (failure / non-claude / reaction) → Task 5. ✓
- Debug-rich manifest (`mode`/`source`/`prompt`/`anchor_url`/`planner_visual_note`/`restaged`/`model`) → Task 5. ✓
- `_clip` consumes prompts; f-strings kept as fallback + example → Task 6 + Task 4. ✓
- Skill revised into the v2 craft bible (de-dup + realism digest + format retune) → Task A. ✓
- Realism rules (flattering texture, faithfulness, even/diffuse light, only-safe negation, product-no-grain) live in the revised skill (system instruction), reconciled from the three realism docs → Task A. ✓
- "Tight" reconciled to dense/front-loaded within Seedance's ~60–100 word effective window → Task A (skill) + Task 3 (user-message order/budget). ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test shows assertions; commands have expected output. ✓

**Type consistency:** `write_motion_prompt(beat, character, window_len, mode, uses_product, anchor_url, timeline_overview, fallback_example)` and `MotionPrompt{prompt, restaged}` are used identically in Tasks 3 and 5. `_beat_mode(beat, *, lipsync_engine)`, `_fallback_prompt(beat, window_len, *, lipsync_engine)`, `_timeline_overview(beats)` match between Tasks 4 and 5. `_write_motion_prompts(...)` kwargs match between Tasks 5 and 6. Manifest key `motion_prompts` and record field names match between Task 5 and the spec. ✓
