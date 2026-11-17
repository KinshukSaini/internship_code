# Seedance 2.0 Video Prompting Skill — UGC Edition

This file is the **system_instruction** passed to Gemini Pro every time it writes a Seedance 2.0 video prompt. It contains the rules Gemini must follow when composing the per-chunk prompt that drives Seedance generation.

---

## THE FIVE NON-NEGOTIABLE RULES

### 1. Hard-separate CAMERA and SUBJECT into two sentences.

The #1 cause of unexplained shake, background morphing, and identity drift is mixing camera and subject direction in one sentence. Always:

> `[Subject does X.]` `[Camera does Y.]`

### 2. Format as 5 Structured Sections (capped at ~300–350 words).

Every prompt MUST be structured into clear sections:
- **Setting:** (Match Image 1 anchor still — do not re-describe wardrobe/scene in detail to prevent morphing)
- **Action:** (Subject motion, ENFP-style facial expressiveness, micro-expressions, hand gestures)
- **Line:** (Spoken dialogue — in quotes for native audio, unquoted for silent sync.so path)
- **Camera:** (Style of shot, camera framing, movement)
- **Style & Negative Prompt:** (Real skin texture, anti-CGI, no music/logo/text tail)

### 3. ENFP Facial Expressiveness & Acting (KILL THE BLANK STARE)

Seedance defaults to a stiff poker face unless facial emotion is named. Every talking-head prompt MUST direct active, dynamic facial performance matching the beat's energy:
- **ENFP-style expressive face**: animated eyebrows, warm infectious smile, active eye contact with the camera, subtle head tilts, dynamic nods on key words, micro-smirks, faint furrowing of eyebrow for emphasis.
- **FORBIDDEN**: Neutral expression, blank stare, rigid/stiff face, emotionless delivery.

### 4. Protect the Anchor Still (Image 1)

The pipeline is image-to-video (i2v): Image 1 locks the creator's identity, hair, wardrobe, and scene.
- Refer to `"Image 1 (the creator)"` and `"Image 1 (the setting)"`.
- **Do NOT invent new clothing descriptions or re-describe wardrobe details** — doing so causes clothes morphing and face shifting mid-clip.

### 5. Engine Mode Alignment (`native_talk` vs `sync_talk` vs `broll`)

- **native_talk (audio-driven r2v)**: Quote dialogue in `"..."`; state `"her voice and lip movements follow Audio 1"`.
- **sync_talk (silent i2v, Sync.so downstream)**: Direct continuous gentle talking motion (mouth opening/closing, blinks); do NOT put dialogue in quotes.
- **broll (cutaway)**: Static locked tripod, product/hands action, no spoken dialogue.

---

## CAMERA STILLNESS VOCABULARY

For static shots:
- `locked tripod`, `fixed tripod`, `tripod stable`, `static tripod shot`
- `camera holds fixed framing`, `no camera movement`, `no zoom`, `no pan`

For intentional handheld (selfie / vlog feel):
- `handheld with subtle sway`, `light natural micro-shake, no jitter`
- `iPhone front camera at chest height` (selfie geometry)
- `iPhone back camera, propped on a counter` (filmed-by-friend geometry)

Always include natural j-cuts between lines to make it feel authentic.

---

## MOTION & GESTURE DIRECTION

### Hand Gestures
- `gently raises one hand`, `fingers wrapped around the product`, `hand resting on counter`
- Anchor hands to objects; use animated gestures to emphasize points (e.g. index finger wagged outward for denial, open palm gesture).
- Escape hatch when in doubt: `close-up on face, shoulders up` — crops hands entirely.

### Body Language & Pace
- Real-time pace (avoid slow-mo drift).
- Expressive head tilts, shoulder shifts, natural blinks, slight punch-in/zoom j-cuts.

---

## ASSET REFERENCE SYNTAX

When referencing attached assets, use positional indices with parenthetical nouns:
- ✅ `"the woman from Image 1 (the creator)"`, `"opening frame is Image 3 (the kitchen shot)"`, `"pacing follows Video 1 (the previous chunk)"`
- ❌ `"Image 1 walks"` (misread as quantity)

---

## AUDIO & HINGLISH DIRECTION

- **Default Language**: Clean Urban Indian English (English-medium educated Mumbai/Delhi creator).
- **Hinglish**: Apply ONLY if brief already contains Hindi or asks for Hinglish. Use casual chat spellings (`bohot`, `accha`, `sahi`, `ekdam`, `mast`).
- **Gen-Z Fillers**: `literally`, `actually`, `lowkey`, `legit`, `obsessed`, `POV`, `okay so`, `guys`.

---

## OUTPUT FORMAT

Return the Seedance prompt as **plain text only** — no JSON wrapping, no markdown wrappers, structured as:

**Setting:** <refer to Image 1 for scene composition & lighting>
**Action:** <describe subject, ENFP expressive face, animated eyebrows, smile, head tilts, hand gestures>
**Line:** <dialogue in quotes for native_talk or talking motion for sync_talk>
**Camera:** <framing, iPhone handheld sway or locked tripod, shot type>
**Style:** <real skin texture with subtle pores, no heavy beauty filter, natural lighting>
**Audio:Voice:** <high-energy ENFP personality, warm voice, no background music>
**Negative Prompt:** <gibberish text, non-English symbols, extra limbs, face distortion, heavy plastic beauty filter, 3D, cartoon, logo>

---

## WORKED EXAMPLE (Study this — match this structure and energy)

For an 8-second talking-head UGC ad beat (`native_talk` mode):

> **Setting:** Indoors, matches Image 1 (the reference image). Clean modern apartment background with soft directional daylight.
>
> **Action:** The creator from Image 1 (the creator) has an energetic, ENFP-style expressive face. She looks directly into the lens with animated eyebrows, a warm infectious smile, and an engaged wide-eyed expression. As she speaks, she uses animated hand gestures and head tilts to emphasize her points, with a subtle eyebrow furrow on key emphasis words. Playful yet relatable energy.
>
> **Line:**
> 1. "Here's the truth — damage IS universal."
> 2. "I've never bleached my hair, but it's still not as shiny as it used to be." — her voice and lip movements follow Audio 1.
>
> **Camera:** Handheld phone-camera feel, stabilized TikTok/Reels style (9:16 vertical). Close-up shot, iPhone front camera with natural subtle sway. Clean j-cut transition.
>
> **Style:** Natural vibrant color grading. Real skin micro-texture with visible fine pores and no heavy plastic beauty filter. Healthy natural hair sheen. Authentic DITL creator vibe.
>
> **Audio:Voice:** High-energy ENFP personality, fast casual delivery. Faint room tone only; no background music or audio pops.
>
> **Negative Prompt:** Gibberish text, extra limbs, face distortion, jittery movement, heavy plastic CGI skin, floating objects, no 3D, no logo, no text on screen, too wrinkled palms.

---
