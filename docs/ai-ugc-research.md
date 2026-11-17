# AI-UGC Video Pipeline: Reference Architecture, Root-Cause Fixes & Improvement Roadmap

> Stack under review: **Gemini script planner → ElevenLabs v3 VO → Nano Banana (Pro) anchor images → Seedance 2.0 single-image I2V → Sync.so lipsync → ffmpeg assembly.**
> This document maps that stack against how serious AI-UGC builders (Creatify, Captions/Mirage, Arcads, Argil, HeyGen, Topview, MakeUGC) actually build, then diagnoses the five pain points from the last real run and gives copy-pasteable fixes.

---

## 1. How serious AI-UGC builders structure their pipelines

Every credible tool converges on the **same five-stage shape** — and your stack maps 1:1 onto it (it is essentially Creatify's "Aurora" pipeline decomposed into open parts):

1. **Ingest** a product URL / brief.
2. **LLM writes MULTIPLE short, casual, hook-first script variants** (not one script). Arcads = 5 hooks × 4 actors × 3 CTAs = 60; Captions Ad Studio auto-generates ~25 ads from one scan. The unit of work is *a batch of variants*, not *a video*.
3. **Audio-first voice model** speaks the script with emotional / non-verbal cues. Audio is generated **before** video, because every modern avatar engine (OmniHuman, Aurora, Sync) derives expression and lip motion **from the audio waveform** — the audio must carry the emotion first.
4. **Audio-driven image-to-video** animates a *single reference portrait* into a **full-body talking performer** — head movement, hand gestures, blinks, breathing, eyebrow raises — not just a mouth. "Half of visual communication is facial expression, micro-reactions and body language, not lip movement" (Captions/Mirage). Cheap mouth-only tools are what produce the rubberized uncanny look.
5. **Heavy edit**: the talking-head is cut to **~10–30% of runtime** (a 90/10 B-roll-to-talking-head split is the common target), interleaved with product/demo B-roll, burned-in native-style captions, and shipped as **dozens of variants** for testing.

### What actually makes output convincing (in priority order)

The thing that separates real-feeling UGC from "the AI look" is **NOT face fidelity**. It is:

1. **The script** — contractions, filler words, mid-thought restarts, hyper-specific detail ("about four weeks now"). AI scripts read like "a press release wearing a hoodie": perfect grammar, no contractions, three-beat parallel structure ("it's fast, it's easy, it's affordable"), buzzwords ("game-changer"), clean sell-y endings. **This is the #1 tell — not hands, not faces.**
2. **Audio-driven emotional expressiveness** across the whole face/upper body, generated from an expressive VO.
3. **Heavy B-roll cutting** so the avatar is rarely on screen long enough to break, with a visual reset every 3–4s and **no shot over ~5s**.
4. **Phone-native imperfection** — handheld framing, casual/imperfect lighting, micro-gestures, "UGC creator, iPhone front camera" framing, slightly messy home backgrounds. "Too perfect" is now itself a tell.
5. **A generate-many → score → regenerate loop** with a human gate on the winner — AI for the cheap testing layer, optional real-creator re-shoot for proven winners.

### One hard architectural constraint to internalize

- **Seedance I2V rejects realistic human faces** on its image-to-video / reference endpoints (real or AI photorealistic). For the human talking-head you must use **Seedance text-to-video** (or a face-permitting model); reserve Seedance **image-to-video for faceless B-roll/product shots**.
- **Sync.so `lipsync-2`/`-2-pro` fail on still frames** — they infer in 2s chunks and need visible speaking motion. Your pipeline correctly uses **`sync-3`**, which builds a global understanding of the whole shot, can open near-still lips, and tolerates handheld motion/angles — but it still produces *generic* lips on a truly static frame, so the input clip must show genuine speaking motion.

---

## 2. Reference pipeline diagram (ideal end-to-end flow)

```
                          ┌──────────────────────────────────────────────┐
                          │  PRODUCT BRIEF / URL                          │
                          └──────────────────────┬───────────────────────┘
                                                 │
                                                 ▼
            ┌──────────────────────────────────────────────────────────────────┐
            │  GEMINI PLANNER  (fan-out: N hook/script variants, not one)        │
            │  • spoken-not-written language (contractions, 1–3 fillers,        │
            │    one self-interruption, ≥1 hyper-specific number)               │
            │  • banned: parallel triplets, buzzwords, press-release endings    │
            │  • prosody baked in: … pauses, — hard cuts, ONE CAPS word/sentence│
            │  • 1–2 inline eleven_v3 audio tags ([laughs]/[excited]) MAX        │
            │  • per-section emotional word-choice arc                          │
            │  • BEAT PLAN: hook(0–3s)→problem→solution→proof→CTA, 3–4s/beat,   │
            │    HARD CAP 5s; talking_head vs broll; mouth-actions → broll only │
            └───────────────────┬──────────────────────────────┬───────────────┘
                                │ full_script (annotated)        │ timeline beats
                                ▼                                │
            ┌──────────────────────────────────┐                │
            │  ELEVENLABS v3  (AUDIO-FIRST)     │                │
            │  stability 0.35 / sim 0.80 /      │                │
            │  style 0.0 / no speaker_boost     │                │
            │  → audio + char-level alignment   │                │
            └───────────────┬──────────────────┘                │
                            │                                    │
                            ▼                                    │
            ┌──────────────────────────────────┐                │
            │  PACING (audio domain, PRE-sync)  │                │
            │  1. trim_silences >0.3s  ──┐      │                │
            │  2. speed_up_vo 1.1–1.3x   │ re-time alignment     │
            │     (NEVER setpts after sync)     │                │
            └───────────────┬──────────────────┘                │
                            │ tightened audio + retimed alignment│
                            ▼                                    ▼
            ┌──────────────────────────────────────────────────────────────────┐
            │  beat_windows()  — map beats to [start,end] on the retimed clock   │
            │  (strip [audio-tag] spans from alignment before mapping/captions)  │
            └───────────────┬──────────────────────────────┬─────────────────────┘
                            │ talking_head beats            │ broll beats
                            ▼                               ▼
        ┌──────────────────────────────┐   ┌──────────────────────────────────┐
        │ NANO BANANA anchor (mid-     │   │ NANO BANANA / product photo       │
        │ speech expression, soft bg,  │   │ anchor (faceless OK)              │
        │ head-and-shoulders crop)     │   └───────────────┬───────────────────┘
        └───────────────┬──────────────┘                   │
                        ▼                                   ▼
        ┌──────────────────────────────┐   ┌──────────────────────────────────┐
        │ SEEDANCE T2V/I2V             │   │ SEEDANCE I2V (B-roll/product)     │
        │ "UGC creator, iPhone front   │   │ physics cues: grip firmly, steady │
        │ cam, handheld, talks         │   │ real-time pace, avoid floating     │
        │ animatedly, mouth open       │   │ energetic; 4–6s clips             │
        │ mid-sentence" — 4–6s, motion │   │ (low-motion QC → regenerate)      │
        │ QC → regenerate if static    │   └───────────────┬───────────────────┘
        └───────────────┬──────────────┘                   │
                        ▼                                   │
        ┌──────────────────────────────┐                   │
        │ SYNC.SO  sync-3              │                   │
        │ lipsync FULL clip vs padded  │                   │
        │ VO slice, THEN trim to window│                   │
        │ sync_mode='silence'          │                   │
        │ (no temperature/occlusion —  │                   │
        │  no-ops on sync-3)           │                   │
        └───────────────┬──────────────┘                   │
                        │ strip Sync audio                  │
                        ▼                                   ▼
            ┌──────────────────────────────────────────────────────────────────┐
            │  FFMPEG ASSEMBLE                                                    │
            │  • trim each clip to its [start,end] slot (Σ == VO duration)       │
            │  • concat talking_head + broll (90/10, reset every 3–4s)          │
            │  • overlay the ONE continuous tightened VO (gapless, no drift)     │
            │  • burn karaoke captions (ON by default — sound-off viewing)      │
            │  • light sound design (whoosh on cuts), end on high-energy cut     │
            └──────────────────────────────────┬─────────────────────────────────┘
                                                ▼
                          ┌──────────────────────────────────────────────┐
                          │  QC LOOP → human gate on winner → publish     │
                          │  (flag: slow-mo, identity drift, lip drift,   │
                          │   mangled hands/labels, leaked logo/music)    │
                          └──────────────────────────────────────────────┘
```

**Master-clock invariant:** the single continuous VO is the clock. Every per-beat trimmed clip must occupy exactly its `[start,end]` slot so `Σ trimmed lengths == VO duration`. All timing transforms (silence-trim, speed-up) happen in the **audio domain before lipsync**; **never** apply `setpts`/`atempo` after Sync.so or the mouth desyncs from the overlaid VO.

---

## 3. Root-cause analysis of the 5 pain points

### Pain 1 — "Voice sounded dead/flat, no expressiveness"

**Root cause (two real bugs in `app/voice.py`):**
- `style=0.55` — for `eleven_v3` ElevenLabs explicitly says **"keep this setting at 0 at all times"**; it adds latency/cost and *mildly destabilizes* (worse on accented IVCs). It is **not** buying youthful energy.
- `use_speaker_boost=True` — **"Speaker Boost is not available for the Eleven v3 model."** It is a silent no-op; the code comment claiming it adds clarity is wrong.
- The deeper cause: **expressiveness in v3 does not come from the sliders at all.** It comes from (a) `stability` in the Creative/Natural band (your `0.35` is already correct — and it is a *continuous* 0.0–1.0 value, **not** quantized to {0/0.5/1.0}; do **not** drop to 0.0 — that risks accent drift/hallucination on Indian IVCs), and (b) **audio tags + CAPS + ellipses in the script text**, which the planner currently never emits. A flat input script → a flat read, regardless of settings.

**Fix:** `VoiceSettings(stability=0.35, similarity_boost=0.80, style=0.0)`, drop `use_speaker_boost`. Then move all energy into the script: Gemini emits `[excited]`/`[laughs]` tags, CAPS emphasis, and `…` beats. → see Quick Win #1 + #2.

### Pain 2 — "Script felt like one straight read with no pauses"

**Root cause:** The `plan_timeline` `### SCRIPT` block (`app/gemini.py` ~line 865) only specifies "casual urban Indian English" + a word count. It gives **no prosody instructions** — no ellipses for pauses, no em-dashes for hard cuts, no CAPS for emphasis, no fillers, no per-section emotional arc. `eleven_v3` reads punctuation as prosody (`…` = weighted pause, `—` = hard cut/trail-off, CAPS = per-word emphasis) and obeys it *more* at low stability (your 0.35 is ideal) — but the script contains none of it, so the model has nothing to inflect. Note: `eleven_v3` ignores SSML `<break>` tags, so pacing **must** come from punctuation/tags.

**Fix:** Add a `### SPOKEN RHYTHM` block instructing `…`, `—`, one CAPS word/sentence, 1–3 fillers, and 1–2 inline audio tags. Because each `dialogue_text` is a verbatim contiguous slice of `full_script` (`_validate_timeline` does an exact `find()`), the punctuation flows straight through to the VO untouched — *as long as the slices preserve it byte-for-byte*. → Quick Win #2.

### Pain 3 — "Character was eating oats WHILE talking → broke lipsync, looked weird"

**Root cause:** This is the universal A-roll/B-roll convention being violated. Real and AI-UGC pipelines **never** put dialogue delivery and a mouth-occupying action in the same shot — they split into A-roll (mouth free, lipsynced) and B-roll (action shown *silently* with VO laid over it). Your architecture already supports this (`talking_head` vs `broll`; only `talking_head` is lipsynced). The gap is a **missing planner constraint + validator**: the `HAND-BUDGET` block routes *some* eating beats to B-roll for hand-count reasons, but a *tripod* talking_head "sips while explaining" passes every existing check — and then feeds Sync.so exactly the occluded/occupied mouth it fails on (warped/double mouths). Technically non-negotiable because `sync-3`/Wav2Lip-lineage models degrade severely on occluded, non-frontal, or mouth-occupied frames.

**Fix:** Hard rule + validator — any mouth-occupying action (eat/bite/chew/sip/drink/swallow/taste/lick/apply-to-lips/spray-in-mouth/hand-over-mouth) **must** be a `broll` beat (`hands_using`/`demo_steps`/`texture_pour`), narrated by VO over silent demo footage. The only legitimate `talking_head`+product overlap is `product_reveal_pivot` (mouth stays free). → Quick Win #3.

### Pain 4 — "Cuts were slow/sluggish when energy was needed"

**Root cause (two layers):**
- **Editing cadence:** beats permit up to 6s and nothing enforces a ceiling. Retention data: a visual reset every ~4s averages **58% retention vs 41%** for static same-length talking-head; ~5s is the safe ceiling, 6s is over it. There is also no silence-trim and no global speed-up, so dead air (TTS pauses >0.3s) and 1.0x playback make everything feel sluggish.
- **I2V motion:** Seedance single-image I2V is **structurally biased toward static/slow output** — the reference image's high-frequency detail makes sampling "lock onto" the still appearance and suppress large motion. The `_SETTLE_CLAUSE` (forces a held pose for the final second) makes it worse, and `_talking_motion_prompt` lacks energy verbs and the "UGC creator, iPhone front camera, handheld" trigger.

**Fix:** Hard-cap beats at 5s (target 3–4s) + validator; add a `pacing.py` stage that trims silences and speeds the VO 1.1–1.3x *in the audio domain before lipsync*; rewrite the Seedance motion prompt with energy verbs and the UGC/iPhone lead-in; ban the bare word "fast" and slow-mo language. → Quick Wins #4 + #5.

### Pain 5 — "Audio drifted out of sync after B-roll sections"

**Root cause:** This is the classic per-clip-audio-accumulation drift. The correct design — which your assembly *mostly* implements — is: lipsync each beat against its exact VO slice, then **strip Sync's audio and overlay ONE continuous gapless VO** at the end, so the voice never glitches at concat seams and small per-clip rounding can't accumulate (`Σ trimmed clip lengths == VO duration`). Drift creeps in when (a) any timing transform is applied **after** lipsync (e.g. speeding a synced clip with `setpts` while the overlaid VO is untouched → instant lip/voice drift), or (b) B-roll clip durations don't sum exactly to their VO windows. The fix is discipline, not a rewrite: do **all** speed/trim work in the audio domain *before* lipsync, keep the continuous-VO overlay, and ensure every trimmed clip exactly fills its slot.

**Fix:** Keep the strip-and-overlay-continuous-VO pattern. Add a docstring guard in `assembly.py` forbidding any post-Sync `setpts`/`atempo`. Apply silence-trim and speed-up as audio-domain transforms on the VO (with alignment re-timing) *before* `beat_windows()`, so every downstream window/slice/clip derives from the same tightened master clock. → roadmap P1/P2 pacing rows.

---

## 4. Prioritized improvement roadmap

| Area | Problem | Fix | File | Priority | Effort |
|---|---|---|---|---|---|
| **Voice expressiveness — settings** | `style=0.55` discouraged for v3 (adds latency, destabilizes accented IVCs); `use_speaker_boost=True` is a no-op on v3; comment is misleading | `VoiceSettings(stability=0.35, similarity_boost=0.80, style=0.0)`, remove `use_speaker_boost`; fix docstring (stability is continuous, 0.35 = expressive band, don't drop to 0.0 on Indian IVCs) | `app/voice.py` (~208–213) | **P1** | small |
| **Voice expressiveness — tags** | Planner never emits `eleven_v3` audio tags, so the VO has no non-verbals/emotion to inflect | Have Gemini emit 1–2 inline tags (`[excited]`,`[laughs]`,`[giggles]`,`[happy gasp]`,`[sighs]`) per script, action-tags only, never stacked-incompatible, each fully inside one beat's slice | `app/gemini.py` (SPOKEN RHYTHM block) | **P1** | medium |
| **Script naturalness** | `### SCRIPT` block lacks spoken-language rules: no contractions/fillers/specific numbers, no ban on triplets/buzzwords | Add: contractions everywhere; 1–3 discourse markers (okay so/honestly/no joke/wait); ≥1 hyper-specific number/timeframe; BAN parallel triplets, buzzwords, press-release endings; "write like you text your best friend" | `app/gemini.py` (~865) | **P0** | small |
| **Script pauses / prosody** | No `…`/`—`/CAPS prosody instructions; risk of literal `<break>` tags (unsupported by v3) | Add `### SPOKEN RHYTHM` block: `…` for pauses, `—` for hard cuts, ONE CAPS word/sentence, sparingly (≤1–2 per section); NEVER emit SSML/`<break>`. Add a guard in `voice.py` stripping any `<break` substring | `app/gemini.py` + `app/voice.py` | **P0** | small |
| **Prosody preservation** | `dialogue_text` slices may normalize punctuation, breaking both prosody and `_validate_timeline`'s exact `find()` | Field desc: "Preserve EVERY character verbatim — ellipses, em-dashes, CAPS, `[audio tags]`; no normalize/re-punctuate/trim" on both `dialogue_text` (~202) and `full_script` (~254) | `app/gemini.py` | **P1** | small |
| **Hook quality** | Hook bullet lacks verbatim example lines per `hook_type` enum | Append one verbatim example per hook_type (pov / curiosity_gap / myth_buster / stop_scrolling / result_first / callout …); hook lands <3s, no brand words | `app/gemini.py` (AD ANATOMY) | **P1** | small |
| **Per-section emotion** | Solution beats can read as "this product offers" (brand voice); no per-section word arc | Per-section vocab: hook=doubt-to-belief; problem=pain words (tried everything/getting worse); solution=DISCOVERY first-person (then I found/finally); proof=specific casual authority; CTA=warm low-pressure. Product is *discovered, never sold* | `app/gemini.py` (AD ANATOMY) | **P1** | small |
| **Action ↔ speech separation** | No rule/validator blocking mouth-occupying actions on `talking_head` beats → breaks lipsync (the oats bug) | Add `### MOUTH-OCCUPYING ACTIONS` rule (forbidden list → route to broll) + `_validate_timeline` branch: raise `ValueError` if `shot_type=='talking_head'` and `visual_note`/`broll_shot` matches MOUTH_ACTION regex | `app/gemini.py` (~277, ~919) | **P0** | medium |
| **Pacing — cut length** | 6s beats permitted; no max-duration enforcement (sluggish feel) | Beat rule "TARGET 3–4s; HARD MAX 5s — never 6s; split into talking_head + broll cutaway"; validator raises if any window >5.5s | `app/gemini.py` (~891, ~277) | **P0** | small |
| **Pacing — dead air** | TTS pauses >0.3s never trimmed; dead air between beats | New `app/pacing.py`: `trim_silences(audio, alignment, -30dB, 0.3s, pad 0.10)` via `silencedetect` + segment-cut + **alignment re-time**; call after `generate_vo` BEFORE `beat_windows`. Never `silenceremove` the muxed output | `app/pipeline.py` + `app/pacing.py` | **P1** | large |
| **Pacing — speed-up** | VO + clips at 1.0x; no energy lift | `speed_up_vo(audio, alignment, factor=1.15)`: ffmpeg `atempo`, rescale alignment `×1/factor`; apply BEFORE lipsync so Sync syncs to fast audio. Docstring forbids post-Sync `setpts`/`atempo` | `app/pipeline.py` + `app/pacing.py` | **P2** | large |
| **Lipsync — input motion** | `_talking_motion_prompt` lacks "mouth opening/closing" + UGC/iPhone trigger; `_SETTLE_CLAUSE` creates near-still tail that degrades sync-3 | Prepend "UGC creator, iPhone front camera, handheld, looks into lens, talks animatedly, mouth clearly opening and closing mid-sentence"; soften/remove `_SETTLE_CLAUSE` for talking_head | `app/pipeline.py` (~144) | **P1** | small |
| **Lipsync — order** | Trims to sub-second window BEFORE lipsync → sync-3 sees a near-still stub → generic lips | Lipsync the FULL ceil-second clip vs a VO slice covering its span, THEN trim output `-ss 0 -t (end-start)` (preserves VO phase). B-roll keeps current path | `app/assembly.py` (~160) | **P2** | medium |
| **Lipsync — dead params** | Risk of adding `temperature`/`occlusion_detection` (no-ops on sync-3); misleading `sync_mode` docstring | Keep `GenerationOptions(sync_mode='silence')` only; comment "sync-3 ignores temperature/occlusion/reasoning"; fix docstring (silence pads audio when audio<video, crops video when audio>video) | `app/lipsync.py` (~55–69) | **P2** | small |
| **Tag/alignment corruption** | If tags are emitted, `alignment.characters` includes literal `[`,`e`,`x`… → `[EXCITED]` burned in captions + drifted beat windows | `strip_alignment_tags(alignment, full_script)` via `re.finditer(r'\[.*?\]')` filtering char indices; use cleaned alignment for `beat_windows` + `words_from_alignment`; defense-in-depth: drop words matching `^\[.*\]$` | `app/captions.py` / `app/assembly.py` | **P1** | medium |
| **I2V motion realism** | Seedance static-bias → floaty/slow; `_DUR_MAX=15` permits sluggish long clips; no low-motion QC | `_DUR_MAX=6` for talking_head; ban bare "fast"/"slow motion"/"static"; energy verbs + physics cues; optional `detect_low_motion()` (ffmpeg `scdet`/`freezedetect`) → regenerate once behind a flag | `app/pipeline.py` (~144, ~377) | **P1** | medium |
| **Authenticity — captions** | `burn_captions=False` by default → proven +12–25% retention lever is off; most views are sound-off | Flip `burn_captions=True` default; verify `MarginV` center-lower (~180–200), cap `words_per_line=2–3` for 9:16 legibility | `app/assembly.py` (~138) | **P0** | small |
| **Authenticity — anchor pose** | Anchors are neutral stills → I2V must infer speaking pose from scratch (drift/hallucination) | Add `mid_speech_talking` expression (mouth slightly open, engaged eyes, animated brows) as default for talking_head anchors | `docs/character-image-skill.md` + `app/gemini.py` | **P2** | small |
| **Authenticity — actor/voice consistency** | Same face/voice not locked across a campaign; backgrounds may be sterile | Lock one face + one voice per character across variants; vary only background/scene; match actor to buyer demographic; slightly-messy home backgrounds | pipeline-level convention | **P2** | small |
| **Authenticity — batch + QC loop** | Pipeline produces one video, not a variant batch with a scored regenerate loop | Make "generate N variants" the default unit; auto-flag hard tells (slow-mo, identity/eye-line drift, lip drift, mangled hands/labels, leaked logo/music); human gate on winner | pipeline-level architecture | **P2** | large |

---

## 5. Quick wins — the 5 highest-leverage changes to do first

### Quick Win #1 — Fix the ElevenLabs v3 voice settings (`app/voice.py` ~208)

Replace the block exactly:

```python
voice_settings = VoiceSettings(
    stability=0.35,        # eleven_v3: CONTINUOUS 0.0-1.0 (NOT discrete). 0.35 = expressive
                           #   Creative/Natural band; keeps audio-tag responsiveness.
                           #   Do NOT drop to 0.0 — risks accent drift/hallucination on Indian IVCs.
    similarity_boost=0.80, # identity anchor for accented IVC (official baseline 0.75)
    style=0.0,             # MUST be 0 on v3 (docs); adds latency + destabilizes. Energy
                           #   comes from audio tags + CAPS + ellipses in the SCRIPT, not here.
)
# NOTE: use_speaker_boost removed — "Speaker Boost is not available for the Eleven v3 model."
```

Also fix the docstring (lines 199–203): expressiveness comes from script-level tags/punctuation, not the sliders.

### Quick Win #2 — Make Gemini write a spoken, prosody-rich, tagged script (`app/gemini.py`)

Add to the `### SCRIPT` block (~865):

```
- Write like ONE real person talking to camera / texting their best friend — never a brand or announcer.
  If a line sounds like an ad read, rewrite it.
- Contractions EVERYWHERE (it's, you're, I've, don't). Mostly short punchy fragments; vary length. Use "you" and "I".
- Include 1-3 natural discourse markers across the whole script (okay so / honestly / no joke / wait / literally) —
  one per section at most, never sprinkled on every line.
- Include at least ONE hyper-specific number or timeframe (e.g. "about four weeks now", "literally 30 seconds").
- BANNED: parallel triplets ("it's X, it's Y, it's Z"), buzzwords (game-changer, revolutionary, unlock),
  clean press-release endings.
- Pick ONE core emotion (relief / excitement / transformation / curiosity). Per section:
  hook = bold doubt-to-belief; problem = pain words (tried everything, getting worse, nothing works);
  solution = DISCOVERY in first person (then I found, finally, completely changed) — product is discovered, NEVER sold;
  proof = casual specificity (a number, a timeframe, "see?"); CTA = warm + direct, low-pressure ("just try it").
```

Add a new `### SPOKEN RHYTHM` block right after:

```
### SPOKEN RHYTHM (the script IS the prosody score)
full_script is read by eleven_v3 TTS, which obeys punctuation as prosody:
- Use … (ellipsis) for a thinking/emotional pause or trail-off  ("I mean… it actually worked").
- Use — (spaced em-dash) for a hard cut or self-correction        ("I'm not kidding — like, life-changing").
- ONE ALL-CAPS word per sentence MAX for emphasis                 ("this ACTUALLY works").
- Use … and — SPARINGLY — at most one or two per section.
- Do NOT emit SSML or <break> tags — eleven_v3 does not support them and will read them literally.
- You MAY place at most 1-2 eleven_v3 audio tags in the WHOLE script for a genuine reaction, placed
  immediately before the words they modify: [laughs], [giggles], [excited], [happy gasp], [sighs] for contrast.
  Action tags only ([laughs] not [amused]); never stack incompatible tags ([whispers][excited]).
  Each tag must sit ENTIRELY within one beat's dialogue_text — never split a tag across beats.
```

Update the `dialogue_text` Field desc (~202) and `full_script` Field desc (~254):

```
Preserve EVERY character of full_script verbatim — including ellipses (…), em-dashes (—),
capitalized emphasis words, and any [audio tags]. Do not normalize, re-punctuate, or trim;
the slice must match full_script character-for-character or it will fail validation.
```

(When you enable tags, also ship Quick-Win-adjacent fix: `strip_alignment_tags` so `[EXCITED]` never lands in captions / never drifts beat windows — see roadmap P1 row.)

### Quick Win #3 — Block mouth-occupying actions on talking-head beats (`app/gemini.py`)

Add this prompt section after `HAND-BUDGET`:

```
### MOUTH-OCCUPYING ACTIONS (CRITICAL for lipsync)
talking_head beats are lip-synced by Sync.so, which requires a clean, frontal, UNOBSTRUCTED mouth.
NEVER assign these to a talking_head beat, regardless of framing:
  eating, biting, chewing, sipping, drinking, swallowing, tasting, licking, applying anything to
  lips/face/mouth, spraying into mouth, brushing teeth, or any hand/object near or over the mouth.
ROUTE all such actions to a broll beat (broll_type = hands_using / demo_steps / texture_pour) so the
VO describes the action over SILENT demo footage.
ALLOWED on talking_head: talking, gesturing, holding the product up (not using it), product_reveal_pivot.
grwm is talking_head ONLY for hair/cheek/eye steps — lipstick/lip application = broll.
```

And add the validator in `_validate_timeline()` (~277):

```python
import re
MOUTH_ACTION = re.compile(
    r"\b(eat|eating|ate|bite|biting|chew|chewing|sip|sipping|drink|drinking|"
    r"swallow|swallowing|taste|tasting|lick|licking|brush(?:ing)? teeth|"
    r"appl\w*\s+(?:to\s+)?(?:lip|lips|mouth|face)|spray\w*\s+\w*\s*mouth)\b", re.I)

for b in beats:
    note = f"{getattr(b, 'visual_note', '')} {getattr(b, 'broll_shot', '')}".lower()
    if b.shot_type == "talking_head" and MOUTH_ACTION.search(note):
        raise ValueError(
            f"talking_head beat has a mouth-occupying action (breaks lipsync): {note!r}. "
            "Route it to a broll beat (hands_using/demo_steps/texture_pour)."
        )
```

### Quick Win #4 — Cap beat length at 5s + energetic, non-static I2V prompt (`app/gemini.py` + `app/pipeline.py`)

In `app/gemini.py` BEAT RULES (~891): change `3-5 seconds` to

```
TARGET 3-4 seconds; HARD MAX 5 seconds per beat — never 6s. If a spoken passage would run 6s+,
split it into a talking_head beat + an immediate B-roll cutaway so the visual resets by 4-5s.
```

Add the validator (after computing windows in `_validate_timeline`):

```python
if (window[1] - window[0]) > 5.5:
    raise ValueError(f"beat window {window} exceeds 5.5s cap — split it.")
```

In `app/pipeline.py`, rewrite `_talking_motion_prompt()` (~144) and soften `_SETTLE_CLAUSE` for talking_head:

```python
def _talking_motion_prompt(self, visual_note: str) -> str:
    return (
        "UGC creator, filmed on iPhone front camera, handheld with slight natural sway, "
        "soft natural daylight on the face. The creator looks directly into the lens and "
        "talks animatedly — mouth clearly opening and closing as if mid-sentence — with lively "
        "expressive hand gestures near the chest, animated eyebrow raises, natural eye blinks and "
        "focus shifts, subtle head bobs and shoulder shifts. "
        f"{visual_note}. Motion is continuous and natural through the FULL clip. "
        "Natural real-time pace. No slow motion, no floating, no static frames. "
        + self._ANATOMY_CLAUSE
    )
```

Ban the dangerous keywords in the Seedance prompt post-processing: strip/replace the literal tokens `fast`, `lots of movement`, `slow motion`, `static`, `cinematic` (alone), `epic`, `beautiful`. Keep the camera the *calm* element (handheld, holds framing) and let the **subject** carry the energy — never make both energetic.

### Quick Win #5 — Turn captions ON by default (`app/assembly.py`)

```python
def assemble(..., burn_captions: bool = True, ...):   # was False
```

Verify in `app/captions.py`: `words_per_line` ≤ 3 and `MarginV` ~180–200 (center-lower third, not the very bottom where UI chrome/thumbs occlude). Rationale to cite in the docstring: Facebook internal study **+12%** view time, A&W Canada **+25%** watch time, Verizon/Publicis 2019 (n=5,616) — **92% of mobile video is watched sound-off**, 80% more likely to finish a captioned video. One extra ffmpeg re-encode pass; the karaoke ASS infrastructure already exists and is driven by the same char alignment.

---

## 6. Sources

**AI-UGC landscape / authenticity**
- https://creatify.ai/blog/why-your-ugc-looks-fake-(and-how-to-fix-it)
- https://creatify.ai/blog/aurora-image-to-video-ai-avatar-model-for-studio-grade-ads
- https://creatify.ai/use-cases/ugc
- https://captions.ai/blog/mirage-worlds-first-foundation-model-for-ugc-video
- https://captions.ai/blog/shaping-voices
- https://www.captions.ai/features/ad-studio
- https://help.captions.ai/docs/project/ai-ads
- https://www.arcads.ai/features/ai-actors
- https://www.arcads.ai/features/ai-lipsync
- https://www.marketermilk.com/blog/arcads-review
- https://www.argil.ai/ugc-video-ads
- https://www.argil.ai/blog/argil-launching-an-ai-ugc-video-generator
- https://www.heygen.com/avatars/avatar-iv
- https://www.heygen.com/academy/heygen-101/ai-lip-sync-technology
- https://www.topview.ai/ai-ugc
- https://www.revid.ai/tools/faceless-video
- https://www.makeugc.ai/
- https://icon.com/
- https://admakeai.com/blog/what-is-ai-ugc-ad
- https://www.campaignasia.com/article/ai-ads-and-the-uncanny-valley-problem/10e51cymjf3wpvkkmxu4qb5gao
- https://metalla.digital/ai-ugc-video-ads-strategies/
- https://www.cliprise.app/learn/guides/model-guides/bytedance-omni-human-complete-guide
- https://www.unite.ai/omnihuman-1-bytedances-ai-that-turns-a-single-photo-into-a-moving-talking-person/
- https://artlist.io/blog/omnihuman-1-5-announcement/
- https://lensgo.ai/blog/seedance-2-vs-arcads-creatify-heygen-best-ai-ugc-ads
- https://a16z.com/ai-avatars/

**ElevenLabs v3 (settings + tags)**
- https://elevenlabs.io/docs/overview/capabilities/text-to-speech/best-practices
- https://elevenlabs.io/docs/best-practices/prompting/eleven-v3
- https://elevenlabs.io/docs/eleven-creative/playground/text-to-speech
- https://elevenlabs.io/docs/eleven-creative/troubleshooting
- https://elevenlabs.io/docs/eleven-creative/voices/voice-cloning
- https://elevenlabs.io/docs/overview/models
- https://elevenlabs.io/docs/api-reference/text-to-speech/convert
- https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps
- https://elevenlabs.io/blog/v3-audiotags
- https://elevenlabs.io/blog/eleven-v3-audio-tags-expressing-emotional-context-in-speech
- https://elevenlabs.io/blog/eleven-v3-audio-tags-precision-delivery-control-for-ai-speech
- https://github.com/elevenlabs/skills/blob/main/text-to-speech/references/voice-settings.md
- https://github.com/elevenlabs/elevenlabs-python
- https://jonathanmast.com/elevenlabs-v3-audio-tags-user-guide-mastering-emotional-voice-control/
- https://www.webfuse.com/elevenlabs-cheat-sheet
- https://runware.ai/docs/models/elevenlabs-v3/guides/directing-with-audio-tags
- https://wavespeed.ai/models/elevenlabs/eleven-v3/timing
- https://audio-generation-plugin.com/elevenlabs-v3/
- https://spokenaac.com/help/how-to-use-elevenlabs-audio-tags-in-spoken/
- https://medium.com/@v-jur-kh/on-text-markup-for-the-elevenlabs-v3-text-to-speech-2b0a330110e1
- https://superscale.ai/learn/sound-effects-emotions/

**UGC scriptwriting**
- https://pixelpanda.ai/blog/2026/03/25/how-to-script-ugc-videos-for-sales/
- https://motionapp.com/blog/how-to-write-ugc-ad-scripts
- https://vidlo.video/blog/a-complete-guide-to-writing-viral-ugc-scripts/
- https://billo.app/blog/ugc-scripts/
- https://billo.app/blog/ai-generated-ugc/
- https://eliteaiadvantage.com/blog/create-ugc-content-ai-tools-arcads-claude
- https://thecopybrothers.com/blog/writing-ugc-scripts-for-ecommerce/
- https://useclip.com/ugc-filming-mistakes-that-ruin-ads

**Pacing / editing rhythm**
- https://www.opus.pro/blog/tiktok-length-format-retention-data
- https://www.opus.pro/blog/ideal-youtube-shorts-length-format-retention
- https://www.opus.pro/blog/tiktok-hook-formulas
- https://www.opus.pro/blog/instagram-reels-hook-formulas
- https://www.ezugc.ai/blog/the-ugc-ad-playbook-formats-hooks-edits-scale
- https://www.stackmatix.com/blog/tiktok-hook-first-3-seconds
- https://www.stackmatix.com/blog/tiktok-ugc-ads-strategy
- https://www.hangryfeed.com/insights/posts/the-psychology-of-the-first-3-seconds-mastering-tiktok-hooks-for-maximum-retention
- https://brandefy.com/optimize-first-3-seconds-video/
- https://www.directai.app/blog/short-form-video-editing
- https://virlo.ai/blog/short-form-video-editing-tips
- https://blitzcutai.com/blog/ugc-video-editing-guide
- https://licensing.routenote.com/blog/how-to-create-effective-jump-cuts-when-video-editing/
- https://www.adobe.com/uk/creativecloud/video/discover/jump-cut.html
- https://scenith.in/tools/video-speed-for-youtube-shorts
- https://www.influencers-time.com/ai-powered-video-hook-analysis-harnesses-kinetic-energy/
- https://soundimages.net.au/blog/mastering-video-length-and-pacing/
- https://www.insidetheedit.com/blog/pacing-in-video-editing
- https://nealschaffer.com/tiktok-video-length/
- https://afterhourscreatorclub.com/guides/how-to-edit-ugc-videos/
- https://zeely.ai/blog/hooks-for-tiktok-video-ads/
- https://www.revenuecat.com/blog/growth/ugc-ads-apps/

**A-roll / B-roll & action-vs-speech separation**
- https://artlist.io/blog/shooting-b-roll-footage/
- https://boords.com/blog/filmmaking-101-a-roll-vs-b-roll-footage
- https://www.nearstream.us/blog/what-is-b-roll-and-how-to-use-it
- https://clipchamp.com/en/blog/how-to-shoot-edit-b-roll-video-ultimate-guide/
- https://www.imagine.art/blogs/ai-ugc-content-creation-playbook
- https://www.qwe.edu.pl/tutorial/ai-lip-sync-face-animation-tools-guide/

**Lipsync (Sync.so) + sync_mode / sync-3 options**
- https://sync.so/docs/models/lipsync
- https://sync.so/docs/models/sync-3
- https://sync.so/docs/product/how-ai-lip-sync-works
- https://sync.so/docs/compatibility-and-tips/improving-lip-sync-quality
- https://sync.so/docs/compatibility-and-tips/media-content-tips
- https://sync.so/docs/api-reference/api/generate-api/create
- https://sync.so/docs/developer-guides/error-handling
- https://sync.so/docs/web-app-guides/speaker-selection
- https://sync.so/docs/tutorials/text-to-speech-lipsync
- https://sync.so/sync-3
- https://sync.so/blog/lipsync-2-0/
- https://sync.so/lipsync-2-pro
- https://ai.sync.so/wav2lip-alternative-solves-wobbly-mouth-diffusion
- https://wavespeed.ai/blog/posts/introducing-sync-lipsync-3-on-wavespeedai/
- https://wavespeed.ai/models/sync/lipsync-3
- https://help.scenario.com/en/articles/sync-3-lipsync-the-essentials/
- https://replicate.com/sync/lipsync-2-pro
- https://fal.ai/models/fal-ai/sync-lipsync/v3
- https://fal.ai/models/fal-ai/sync-lipsync/v2
- https://fal.ai/learn/devs/sync-lipsync-react-1-paramter-guide
- https://runware.ai/docs/models/sync-lipsync-2
- https://lipsync.com/compare/heygen-vs-hedra
- https://lipsync.com/guides/lip-sync-quality-checklist
- https://www.theinfluencer.ai/blog/fix-ai-lip-sync-video
- https://github.com/synchronicity-labs/sync-python-sdk

**Seedance 2.0 / I2V motion prompting**
- https://fal.ai/models/bytedance/seedance-2.0/image-to-video
- https://seed.bytedance.com/en/blog/official-launch-of-seedance-2-0
- https://help.apiyi.com/en/seedance-2-0-prompt-guide-video-generation-camera-style-tips-en.html
- https://seedance2.ai/guide
- https://videoai.me/blog/seedance-2-0-ugc-prompts
- https://www.atlabs.ai/blog/how-to-make-ugc-ads-with-a-single-prompt-using-seedance-2.0-(15-prompts)
- https://ugcmaker.org/blog/detail/How-to-Use-Seedance-2-to-Create-AI-UGC-Ads-That-Actually-Feel-Native-9ab7a3fde02b/
- https://github.com/ZeroLu/awesome-seedance
- https://starpop.ai/blog/articles/how-to-make-ai-ugc-videos-seedance-2-0
- https://www.cliprise.app/learn/guides/best-practices/video-duration-limits-5s-10s-15s
- https://help.scenario.com/en/articles/choose-the-right-video-model/
- https://help.runwayml.com/hc/en-us/articles/39789879462419-Gen-4-Video-Prompting-Guide
- https://www.atlascloud.ai/blog/guides/mastering-kling-3.0-10-advanced-ai-video-prompts-for-realistic-human-motion
- https://medium.com/@creativeaininja/how-to-actually-control-next-gen-video-ai-runway-kling-veo-and-sora-prompting-strategies-92ef0055658b
- https://zsky.ai/blog/ai-hands-fix-guide
- https://www.gensgpt.com/blog/ai-hands-anatomy-body-fixes-common-errors-2026-guide
- https://fluxnote.io/blog/minimax-hailuo-video-generation-honest-review-2026
- https://akool.com/blog-posts/minimax-hailuo-video-prompt-guide
- https://www.apatero.com/blog/avoid-slow-motion-wan-22-video-generation-2025
- https://arxiv.org/pdf/2506.08456
- https://arxiv.org/pdf/2412.05848
- https://arxiv.org/pdf/2402.04324
- https://arxiv.org/html/2601.22127v1

**Fake-tells, QC loop & captions evidence**
- https://digitalsynopsis.com/tools/ai-videos-look-fake-how-to-fix/
- https://percify.io/blog/5-reasons-ai-avatar-videos-look-unnatural-and-how-percify-fixes-it
- https://uncannyai.io/blog/how-to-fix-ai-video-audio
- https://hailuoai.video/pages/blog/uncanny-valley-effect-ai-video-explained
- https://www.mimicminds.com/post/uncanny-valley-explained
- https://alici.ai/blog/best-ai-ugc-ad-generators
- https://higgsfield.ai/blog/3QU3e0LGo6VHDMEQGwdGv1
- https://arapahoelibraries.org/blogs/post/how-to-spot-fake-ai-generated-products/
- https://community.heygen.com/public/resources/how-i-scale-realistic-ugc-video-ads-with-ai-avatars-2025-05-15
- https://appblueprint.substack.com/p/wait-that-ugc-video-was-ai
- https://redis.io/blog/ai-human-in-the-loop/
- https://designrevision.com/blog/best-ai-ugc-tools
- https://ppc.io/blog/ai-ugc
- https://inbeat.agency/blog/ai-ugc-ads
- https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12166545/
- https://www.3playmedia.com/blog/verizon-media-and-publicis-media-find-viewers-want-captions/
- https://www.3playmedia.com/blog/studies-find-captions-improve-engagement/
- https://www.streamingmedia.com/Articles/ReadArticle.aspx?ArticleID=131860

**Pacing engineering (ffmpeg / sync tolerances)**
- https://dev.to/dak425/automatically-trim-silence-from-video-with-ffmpeg-and-python-2kol
- https://ayosec.github.io/ffmpeg-filters-docs/8.0/Filters/Audio/silenceremove.html
- https://ffmpeg.org/pipermail/ffmpeg-user/2021-September/053520.html
- https://renderio.dev/blogs/ffmpeg-speed-up-video/
- https://longstories.ai/blog/best-practices-ai-voice-lip-sync-consistency
- https://en.wikipedia.org/wiki/Audio-to-video_synchronization
- https://blog.gdeltproject.org/using-ffmpegs-scene-detection-to-generate-a-visual-shot-summary-of-television-news/
- https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/vf_freezedetect.c
- https://ffmpeg.org/ffmpeg-filters.html
- https://donaldfeury.xyz/remove-the-silent-parts-of-a-video-using-ffmpeg-and-python/
