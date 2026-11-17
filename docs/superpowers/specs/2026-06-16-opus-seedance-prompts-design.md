# Opus-written Seedance motion prompts (v2)

**Date:** 2026-06-16
**Status:** Design — awaiting review

## Motivation

The v2 pipeline (`generate_ugc_video` in `app/pipeline.py`) generates each beat's
Seedance motion prompt from hardcoded Python f-strings (`_talking_motion_prompt`,
`_native_talking_prompt`, `_broll_motion_prompt`). `video-seedance-skill.md` — the
canonical craft doc for writing a good Seedance prompt — is **not** used anywhere in
v2; it is only loaded by the legacy v1 `GeminiClient.write_seedance_prompt`
(`gemini.py:1916`), reachable only from the old Gradio wizard tab. In v2 the skill
file is passed to `GeminiClient` (`pipeline.py:600`) but never read at runtime.

We want Opus to write each scene's Seedance prompt, guided by the skill, grounded in
the rendered anchor frame, so the prompts are **resourceful** — densely exploiting the
image + skill + planner intent, every clause earning its place, no generic
boilerplate. "Tight" here means resourceful and well-crafted, **not** short.

## Goal

For each beat, have **Claude Opus** (`claude-opus-4-8`) write the Seedance motion
prompt using `video-seedance-skill.md` as the system instruction, the rendered anchor
image as multimodal grounding, and the planner's per-beat intent + full-timeline arc
as context. Generate all beats **concurrently**. Fall back to the existing f-strings
on failure.

## Non-goals

- Removing the f-string builders (they stay: live fallback + "bar to beat" reference).
- Changing the v1 path or `write_seedance_prompt`.
- Changing anchors, voice, split, or assembly stages.

## Decisions (resolved)

| Decision | Choice |
|---|---|
| Provider | Claude Opus (`tier="pro"`), per "use Anthropic". |
| Grounding | **Multimodal** — feed the anchor frame, plus full planner context so Opus doesn't re-describe what's locked. |
| Realism guards | Opus **owns the whole prompt**; the realism rules (flattering skin texture, `preserve composition and colors`, even/diffuse lighting, the only-safe `no 3D/cartoon/VFX` negation, product-no-grain, anatomy) live in the **revised skill** (system instruction) + the mode's f-string handed as a "bar to beat" example. Not hard-appended. |
| Skill file | **Revised** (Task A) into the v2 craft bible: de-duplicated, realism digest folded in, length/format retuned for tight per-beat i2v. Was previously treated as immutable. |
| "Tight" | **Resourceful = dense and front-loaded, not merely short.** Reconciled with the realism docs: Seedance down-weights trailing tokens, so ~60–100 words is the effective window, ordered motion → camera → texture/faithfulness → short constraint tail. Longer ≠ better; no padding, but no hard truncation either. |
| Staging authority | Honor the planner's `visual_note` **by default**; full latitude to re-stage / change the scene **only when Opus judges it genuinely required** (anchor contradicts the note, or the planned action would read unnatural). |
| Beat context | **Timeline-aware** — each call sees the full beat list + script arc (shared cached prefix), so motion/energy flow across cuts. |
| Fallback | On retry-exhausted failure, non-Claude provider, or missing key → that beat falls back to its f-string builder. The run never dies on one bad sample. |
| Rollout | Wire directly into the pipeline. |
| Reaction beats | Skip Opus — silent holds have no motion worth an LLM call; keep the cheap f-string path. |

## Architecture

A new concurrent stage **5b** between anchors (stage 5) and clips (stage 6):

```
5.  anchors        → anchors[i].url              (exists)
5b. motion prompts → motion_prompts[i]           (NEW: concurrent, multimodal, Opus)
6.  clips          → _clip() consumes motion_prompts[i]   (was: f-string inline)
```

Stage 5b runs after anchors because the call is multimodal — it needs `anchors[i].url`.
It mirrors the existing anchor-prompt fan-out (`pipeline.py:863`): build one thunk per
beat, run via `_gather_with_retry`, persist results to the manifest.

### Caching / concurrency

The shared, per-job prefix is cached so the concurrent per-beat calls are cheap. The
`system_instruction` is a single block — `skill_md` + the full-timeline overview,
clearly delimited — which `claude.py:72` already wraps in `cache_control: ephemeral`.
No change to `generate()`'s caching is needed.
- **Cached system block** = `video-seedance-skill.md` + the timeline overview (each
  beat's index, ad_section, shot_type, dialogue, script arc). Identical across the
  fan-out within one job → first call writes the cache, the rest read it.
- **Per-beat varying tail** (user content) = the anchor image block + "THIS is beat N"
  specifics + mode task.

(A second cache breakpoint to keep the static skill cached *across* jobs is deliberately
skipped — YAGNI; the 5-min TTL makes cross-job reuse marginal and it would add API
surface to `generate()`.)

### Components

**`app/claude.py` — `ClaudeBackend.generate`**
Add an optional `image_url: str | None`. When set, the user message content becomes a
list: `[{"type": "image", "source": {"type": "url", "url": image_url}}, {"type":
"text", "text": prompt}]`. The anchor's hosted URL is the same one already handed to
Seedance as an image input, so it is known-public — no extra download. Text-only calls
(every existing caller) keep passing a string; behavior unchanged.

**`app/gemini.py` — `_generate` seam**
Thread an optional `image_url` through to `self._claude.generate(...)`. The Gemini
branch ignores it for now (this feature is Claude-only; fallback covers the
gemini-provider case).

**`app/gemini.py` — new `write_motion_prompt(...)`**
Async method, shaped like `write_seedance_prompt` (`gemini.py:1916`):
- `system_instruction = self._load_skill_md()`
- `tier="pro"` → Opus
- passes `image_url=anchor_url`
- wrapped in `_with_retry` (label `"write_motion_prompt"`)
- builds the user message from beat + character + mode (below)

Signature (sketch):
```python
async def write_motion_prompt(
    self, *, beat, character, window_len: float,
    mode: Literal["sync_talk", "native_talk", "broll"],
    uses_product: bool, anchor_url: str,
    timeline_overview: str,            # shared, cached prefix
    fallback_example: str,             # the mode's f-string output
) -> str: ...
```

**`app/pipeline.py` — stage 5b + `_clip` consumption**
- Compute `mode` per beat from the same logic `_clip` already uses
  (`pipeline.py:905-928`): `native` → `"native_talk"`; talking_head non-reaction →
  `"sync_talk"`; else `"broll"`. Reaction beats are marked skip.
- Build `timeline_overview` once from `beats`.
- Fan out `write_motion_prompt` per non-reaction beat via `_gather_with_retry`; each
  thunk catches its own terminal failure and substitutes the f-string for that beat.
- Reaction beats and any fallback beats use the existing f-string builder.
- Persist `manifest["motion_prompts"]` (list keyed by index) — debug-rich, not just
  the string — written atomically as each beat lands (same as `clips`,
  `pipeline.py:954`). `_Manifest` is write-only-forward; this is for debugging +
  external resume scripts (e.g. `resume_assemble.py`), not in-function reuse — the
  anchor/clip stages don't reuse on re-run either.
- `_clip` reads `motion_records[i]["prompt"]` instead of calling the f-string inline.

### Manifest record (for later debugging)

Each beat's entry persists enough to reconstruct what the clip was asked to do and why:

```json
{
  "index": 3,
  "mode": "sync_talk",            // sync_talk | native_talk | broll | reaction
  "source": "opus",              // opus | fallback (failure/non-claude/no-key) | reaction
  "prompt": "<final prompt sent to Seedance>",
  "anchor_url": "<the frame Opus saw>",
  "planner_visual_note": "<the note Opus was given>",
  "restaged": true,               // did Opus override the planner's staging?
  "model": "claude-opus-4-8"     // null on fallback
}
```

`source` and `restaged` are the key debug signals: they tell you, after the fact, which
clips used an Opus prompt vs. a fallback f-string, and where Opus chose to deviate from
the planner. Written atomically at stage-5b completion (same `_Manifest` pattern as
`anchors`, `pipeline.py:875`).

## User-message construction (per beat)

Assembled in `write_motion_prompt`. Sections:

1. **Anchor lock**: "Image 1 is the already-rendered opening frame for this shot.
   Scene, setting, wardrobe, props, and creator identity are LOCKED by this image. Do
   NOT re-describe them — direct only MOTION, CAMERA, and AUDIO. Re-describing what's
   already in frame makes the model morph it."
2. **Creator identity**: the locked `CharacterDescription` (age/gender/ethnicity/hair/
   face/wardrobe/setting/energy).
3. **Planner intent for THIS beat**: index, ad_section, shot_type, framing /
   broll_type / broll_shot, `visual_note`, beat_kind, `window_len`, uses_product.
4. **Staging authority clause**: "Honor the planner's visual_note by default. You may
   re-stage or change the action ONLY if the anchor contradicts it or the planned
   action would look unnatural/glitchy — otherwise keep the planner's intent."
5. **Mode task** (the one branch that matters):
   - `sync_talk`: continuous talking motion, mouth opening/closing through the clip,
     gestures begin AND resolve within `window_len`s; **no dialogue text** (Sync.so
     adds the mouth downstream).
   - `native_talk`: the line **in quotes** + "voice and lip movements follow Audio 1";
     no gesture-window budget (reference audio paces the take).
   - `broll`: the `visual_note` action + ONE camera move; **static locked-off camera**
     for product/hands types.
6. **Realism guidance (reference, not mandate)**: skin-texture realism, preserve-the-
   anchor faithfulness, exactly-two-hands anatomy, product stillness; plus the mode's
   current f-string as an example of the quality bar to beat. Opus decides how to fold
   these in.
7. **Output contract**: "Be resourceful — exploit the image, the skill, and the
   context fully. Every clause does work; cut filler, not signal. Camera and subject in
   separate sentences. No artificial length cap. Return the prompt as plain text only —
   no commentary, no markdown."

## Data flow

```
beats[] + character + windows[] + anchors[].url
   │
   ▼ stage 5b (concurrent fan-out, multimodal Opus)
write_motion_prompt(beat_i, anchor_i.url, timeline_overview, mode_i, ...)
   │   system = skill_md (cached)
   │   user   = timeline_overview (cached) + anchor image + beat-i specifics
   │   on failure → f-string for beat_i
   ▼
motion_prompts[i]  ──persist──▶ manifest["motion_prompts"]
   │
   ▼ stage 6
_clip(i) uses motion_prompts[i]  ─▶ Seedance request
```

## Error handling

- Each per-beat call is wrapped in `_with_retry` (3 attempts, transient-aware,
  including Anthropic transients — `gemini.py:846-853`).
- The stage 5b thunk catches a terminal failure for a single beat and substitutes that
  beat's f-string, logging via `on_progress`. One bad beat never aborts the run.
- Non-Claude provider or missing `ANTHROPIC_API_KEY`: stage 5b short-circuits to
  f-strings for all beats (preserving today's behavior).
- A refusal (`claude.py:108`) surfaces as a `ValueError` → retried, then f-string
  fallback.

## Verification

- Unit: `write_motion_prompt` builds the right user message per mode (sync/native/
  broll); fallback substitutes the f-string when the Opus call raises; reaction beats
  skip Opus.
- `ClaudeBackend.generate` builds image content only when `image_url` is set; text-only
  callers unchanged.
- Integration: a real `generate_ugc_video` run (wire-in rollout) — confirm
  `manifest["motion_prompts"]` is populated, clips consume them, and a forced failure
  on one beat falls back without aborting.
- Manifest: after a run, `manifest["motion_prompts"]` lists every beat with its
  `source`/`restaged`/`prompt` (readable by external resume/debug scripts).

## Files touched

- `app/claude.py` — optional `image_url` on `generate()`.
- `app/gemini.py` — thread `image_url` through `_generate`; add `write_motion_prompt`.
- `app/pipeline.py` — stage 5b (concurrent + manifest-persisted + timeline overview);
  `_clip` consumes `motion_prompts[i]`; per-beat fallback.
- `video-seedance-skill.md` — unchanged.
- `video-seedance-skill.md` — **revised** (Task A): de-duplicated, realism digest folded in, length/format retuned for tight per-beat i2v, worked example replaced. Craft kept universal so the legacy v1 `write_seedance_prompt` still works.
- f-string builders — unchanged (fallback + reference example). They already encode the shipped realism clauses (`_SKIN_TEXTURE_CLAUSE`, `_FAITHFULNESS_CLAUSE`, `_ANATOMY_CLAUSE`, even/diffuse lighting), so they remain a faithful "bar to beat" for Opus.
