@# Seedance 2.0 i2v Realism Guide — Killing the Plastic/Waxy Look

*For our EvoLink → ByteDance Seedance 2.0 image-to-video UGC pipeline. Every recommendation is grounded in our actual code (`app/pipeline.py`, `app/video_gen.py`, `app/config.py`) and reconciled against adversarial source verification — folklore is flagged, not shipped.*

*Research method: 5 parallel web-research agents (48 techniques gathered) → 14 load-bearing/folklore-risk claims adversarially fact-checked against primary sources → synthesis. This supersedes `docs/seedance-prompting-guide.md`, which specced the right direction but was never wired into the live prompts and contained a few unverified phrasings corrected here.*

---

## 1. Root-Cause Diagnosis: Why It's Still Plastic at 1080p

The waxy/over-polished look has **four contributing causes**, but they are NOT equal. Ranked by leverage:

| # | Cause | Leverage | Why |
|---|-------|----------|-----|
| **1** | **The anchor still is the realism ceiling** (Nano Banana Pro → `image_urls[0]`) | **Dominant** | i2v is condition-preserving: frame 1 *is* the input still, and frames 2..N regress toward it. Seedance cannot manufacture skin physics the still never encoded. If the anchor reads "digital/retouched," every generated frame amplifies that. ([andyhtu.com](https://andyhtu.com/seedance-2-plastic-skin-how-to-fix-it/) — *confirmed*; [scenario i2v guide](https://help.scenario.com/en/articles/choose-the-right-video-model/)) |
| **2** | **The Seedance motion prompt says NOTHING about skin/texture/optics** | **High, zero-cost** | Frames 2..N are *re-synthesized*; the denoiser fills prompt-silence with its smooth "averaged-beauty" prior because pore/vellus-hair detail is mathematically noise-like and gets removed. Our `_talking_motion_prompt` / `_native_talking_prompt` / `_broll_motion_prompt` contain zero texture language. `docs/seedance-prompting-guide.md` already specs the fix — it was **never wired in**. ([fal t2v default prompt uses "natural pores and subsurface scattering"](https://fal.ai/models/bytedance/seedance-2.0/text-to-video) — *confirmed*) |
| **3** | **No post-process "real-capture" finishing layer on the output video** | **Medium** | Clean AI renders lack grain, sensor noise, vignette, codec signature — and that *absence* is itself the AI tell. This is a per-frame video step, not a still step. ([zayne.io ffmpeg](https://zayne.io/articles/vintage-camera-filters-with-ffmpeg)) |
| **4** | **Resolution (720p default)** | **Lowest — necessary, not sufficient** | 720p clips pore-detail below the floor, but the user already proved 1080p stays plastic. Higher res *amplifies* existing face artifacts; it lets texture survive but cannot create it. ([magichour settings](https://magichour.ai/blog/seedance-20-best-settings) — *confirmed*) |

**Honest caveat on the anchor (#1):** The strongest claim — "our Nano Banana anchor is too clean, de-slick it" — is **partially refuted by our own code**. `docs/character-image-skill.md` already mandates "visible but subtle pore detail," subsurface scattering, "not hyper-sharp" irises, and already **bans** `8K/4K/masterpiece` tokens. So the anchor prompt is in *better* shape than the motion prompt. The de-slick mechanism ("film-statistics / out-of-distribution") is **practitioner folklore, not ByteDance-documented** — two independent testers (Andy Tu, WaveSpeed) agree on the *direction*, but the cited "magichour confirms" is **false** (magichour says the opposite: use clean high-res inputs). **Conclusion: fix #2 first (it's a verified gap), inspect a live anchor before assuming #1, and treat anchor de-slick as an A/B-gated experiment, not a fact.**

---

## 2. Ranked Realism Levers

Tags: `[PROMPT]` = Seedance motion prompt · `[PARAM]` = payload key · `[IMAGE]` = anchor/Nano-Banana · `[POST]` = ffmpeg after generation.

| Rank | Lever | Tag | Confidence | Source |
|------|-------|-----|-----------|--------|
| 1 | **Wire the skin-microtexture + sensor/optics tail into the 3 live prompts** (currently unshipped) | `[PROMPT]` | **High** | [fal t2v](https://fal.ai/models/bytedance/seedance-2.0/text-to-video), [andyhtu](https://andyhtu.com/fixing-plastic-ai-skin/) |
| 2 | **Add a first-frame faithfulness clause** ("preserve composition and colors of the reference image") | `[PROMPT]` | High (anti-drift) / Medium (anti-plastic) | [apiyi](https://help.apiyi.com/en/seedance-2-0-prompt-guide-video-generation-camera-style-tips-en.html), [10b.ai](https://10b.ai/blog/seedance-2-0-prompt-tips-motion) |
| 3 | **Add POSITIVE micro-texture cues to the anchor prompt** (pores, vellus hair, T-zone oil) + fix the self-contradicting "no fake imperfections" wording | `[IMAGE]` | High | [andyhtu](https://andyhtu.com/seedance-2-plastic-skin-how-to-fix-it/), [claid](https://claid.ai/blog/article/fix-ai-skin-texture) |
| 4 | **Fix the talking-head lighting clause** — swap "soft daylight" for even/diffuse + constant exposure (kills skin shimmer-in-motion) | `[PROMPT]` | High | [wavespeed flicker](https://wavespeed.ai/blog/posts/blog-fix-flicker-jitter-seedance-2-0/) |
| 5 | **Post-generation ffmpeg "phone capture" pass** (subtle grain + luma noise + faint vignette + one H.264 recompress) | `[POST]` | High | [zayne.io](https://zayne.io/articles/vintage-camera-filters-with-ffmpeg) |
| 6 | **Add a short inline anti-CGI ban tail** — `no 3D, no cartoon, no VFX` ONLY (the one verbatim-sourced triad) | `[PROMPT]` | Medium | [higgsfield](https://higgsfield.ai/blog/seedance-prompting-guide), [imagine.art](https://imagine.art/blogs/seedance-2-0-prompt-guide) |
| 7 | **1080p on standard model for hero finals** (after 1–6) | `[PARAM]` | High (sharpness only) | [magichour](https://magichour.ai/blog/seedance-20-best-settings) |
| 8 | **Keep clips 4–6s, integer duration** (already done) | `[PARAM]` | Medium | [magichour](https://magichour.ai/blog/seedance-20-best-settings) |
| 9 | **Optional: de-slick the anchor before upload** (very light grain + ≤5% soften) — A/B-gated, folklore mechanism | `[IMAGE]` | Mixed | [andyhtu](https://andyhtu.com/seedance-2-plastic-skin-how-to-fix-it/), [wavespeed](https://wavespeed.ai/blog/posts/blog-fix-flicker-jitter-seedance-2-0/) |

---

## 3. Copy-Paste Prompt Building Blocks

**Budget reality (verified):** the official cap is ~60–100 words and trailing tokens get down-weighted ([apiyi](https://help.apiyi.com/en/seedance-2-0-prompt-guide-video-generation-camera-style-tips-en.html)). Our talking-head body (~60w) + `_ANATOMY_CLAUSE` (~33w) is already ~93w. **You cannot just append 32 words.** Solution: a TRIMMED ~22-word texture clause, placed *before* the anatomy guard (not buried at the end), and drop the lowest-value optics tokens (sensor noise, focus breathing) first.

### New shared constants to add to `app/pipeline.py`

```python
# Skin micro-texture + light sensor optics. Frames 2..N are re-synthesized and
# regress to a smooth prior unless texture is NAMED. Trimmed to ~22 words to
# stay inside the ~100-word cap; placed ahead of _ANATOMY_CLAUSE, not at the
# tail, because trailing tokens are down-weighted. NO "no plastic skin" — that
# negates the subject noun and can BACKFIRE (no negative_prompt field exists).
_SKIN_TEXTURE_CLAUSE = (
    "Natural skin texture with visible pores, subtle vellus hair and gentle "
    "subsurface scattering, unretouched; phone-sensor capture with subtle "
    "film grain and soft highlight rolloff."
)

# First-frame faithfulness: anti-drift / anti-morph. Attested wording only
# ("preserve composition and colors") — NOT the invented "...and lighting of
# the first frame" composite. Lighting is handled by the (fixed) light clause.
_FAITHFULNESS_CLAUSE = (
    "Preserve the composition and colors of the reference image."
)

# The ONE directly-sourced anti-CGI triad (Higgsfield/imagine.art, verbatim).
# Do NOT add "no plastic/waxy skin/beauty filter" — invented + backfire-prone.
_ANTI_CGI_TAIL = "No 3D, no cartoon, no VFX look."
```

### 3a. `_talking_motion_prompt` (Sync.so / silent-i2v path)

**Already there:** iPhone trigger, per-section tone, micro-gesture, mouth-motion clause, gesture window, real-time-speed, anatomy. **Add:** texture clause, faithfulness, fixed lighting, anti-CGI tail.

```python
return (
    "Filmed on an iPhone front camera, handheld with a slight natural sway, "
    "even soft diffuse daylight on the face, steady constant exposure. "      # CHANGED from "soft daylight"
    f"The creator talks to the lens the whole time, {tone}, with {micro}; "
    "mouth clearly opening and closing mid-sentence and natural blinks. "
    f"Gestures begin and resolve within the first {window_len:.1f} seconds. "
    "Motion stays continuous and natural at real-time speed for the full clip. "
    f"{_SKIN_TEXTURE_CLAUSE} {_FAITHFULNESS_CLAUSE} {_ANATOMY_CLAUSE} "        # ADDED texture + faithfulness
    f"{_ANTI_CGI_TAIL}"                                                       # ADDED
)
```
*Lighting change rationale: WaveSpeed says swap warm/directional light for even/diffuse + constant exposure to stop skin "breathing"/shimmer ([source](https://wavespeed.ai/blog/posts/blog-fix-flicker-jitter-seedance-2-0/)). Do NOT lock the camera — handheld UGC sway is intentional and `camera_fixed=False` ships for talking heads.*

### 3b. `_native_talking_prompt` (audio-driven r2v path)

**Already there:** iPhone trigger, quoted line, tone, real-time pace, Audio-1 binding, anatomy. **Add:** texture, faithfulness, anti-CGI. **Do NOT add `no music`** — r2v owns the audio track.

```python
return (
    "Filmed on an iPhone front camera, handheld with a slight natural sway, "
    "even soft diffuse daylight on the face. "                                # CHANGED
    f'The creator looks at the lens and says: "{line}" — {tone}. '
    "She speaks the full line naturally at real-time pace. "
    "Her voice and lip movements follow Audio 1. "
    f"{_SKIN_TEXTURE_CLAUSE} {_FAITHFULNESS_CLAUSE} {_ANATOMY_CLAUSE} "        # ADDED
    f"{_ANTI_CGI_TAIL}"                                                       # ADDED
)
```

### 3c. `_broll_motion_prompt`

**Critical split:** grain dirties product labels. **Lifestyle b-roll → full texture/grain tail. Product/hero b-roll → NO grain**, use material-reflection language instead.

```python
if broll_type in _STATIC_CAMERA_BROLL_TYPES:
    camera = "The camera holds completely still on a locked-off tripod"
    hold = _PRODUCT_HOLD_CLAUSE
    texture = "Realistic material reflections and natural surface texture."   # PRODUCT: no grain
else:
    camera = ("Gentle handheld camera movement at a steady real-time pace, "
              "phone-camera feel")
    hold = _SETTLE_CLAUSE
    texture = _SKIN_TEXTURE_CLAUSE                                            # LIFESTYLE: full grain tail
return (
    f"{broll_shot}. {visual_note}. "
    f"{camera}. {_PRODUCT_STABILITY_CLAUSE} {hold} "
    f"{texture} {_FAITHFULNESS_CLAUSE} {_ANATOMY_CLAUSE}"                     # ADDED texture + faithfulness
)
```
*Anti-CGI tail omitted on product hero to keep the label clean and the prompt short; add it only to lifestyle if desired.*

### 3d. Anchor prompt additions (`app/ui.py` / `gemini.py`)

Add **positive but FLATTERING** micro-texture cues. These are attractive influencers — the goal is *clear, beautiful, real* skin that breaks the waxy CGI sheen, NOT gritty hyper-realism. **Do not** lean on pore/oil/peach-fuzz emphasis (`visible pores`, `T-zone oil`, `tonal unevenness`, `vellus hair`) — that reads blotchy/greasy/haggard on a good-looking creator. Use just enough *fine* texture:
```
real, non-airbrushed skin with fine natural texture and subtle pores —
clear, healthy, dewy and attractive influencer skin, not airbrushed-plastic
or over-smoothed
```
And **fix the self-inflicted contradiction**: the anchor prompt's "no fake textures" / "no fake imperfections" can suppress the fine texture we want. Reword to `no plastic-CGI texture` — do **not** ban "imperfections" wholesale.

> **Calibration note (user feedback):** the plastic fix must stay subtle and flattering. The tell to remove is *waxy/CGI smoothness*, and a small amount of fine, even texture is enough. Over-naming pores/oil/unevenness swings too far the other way and makes a beautiful creator look unattractive. Same principle in the video `_SKIN_TEXTURE_CLAUSE`: "clear, healthy, attractive skin with fine natural texture, subtle pores" — not "visible pores, vellus hair, unretouched."

---

## 4. Fix the Anchor Still + Post-Process the Video

### Anchor still (`[IMAGE]`)
1. **Verify before acting.** Pull one live anchor PNG and inspect it. Our `character-image-skill.md` already enforces pores/subsurface/anti-plastic — if the still looks genuinely airbrushed, *strengthen the prompt* (Section 3d) before touching pixels.
2. **Confirm max native resolution.** Check `image_gen.py _config_for_tier` passes Nano Banana Pro's largest `image_size` for the hero anchor (pores must be *drawn* at gen time, not recovered downstream).
3. **Optional de-slick (A/B-gated, folklore mechanism).** If still plastic after the motion-prompt fix, run a *conservative* pass — even exposure, normalize color, light monochrome grain, **≤5% soften (NOT 10%), never over-sharpen**. Over-denoise/over-blur is itself a cause of waxy skin, so the safe band is narrow. Use the `image-gen:image-post-processing` skill. Integration point: between Nano Banana output and `request.first_frame_url` (lands at `image_urls[0]`, `app/video_gen.py:298`). **Validate empirically** — keep only if visibly less plastic at a pinned seed.

> **Do NOT pre-bake film grain into the still.** i2v over-conditions on high-frequency input detail and takes a "static shortcut" — grain gets frozen/smoothed AND suppresses motion ([arXiv 2506.08456](https://arxiv.org/html/2506.08456v1)). Grain belongs on the *output video*, not the conditioning still. (Distinct from the ≤5% *soften* above, which is structural, not high-freq noise.)

### Post-process the video (`[POST]`) — `app/assembly.py`, after lipsync/stitch, ONCE
Subtle, modern-phone (not vintage). Verified filtergraph ([zayne.io](https://zayne.io/articles/vintage-camera-filters-with-ffmpeg)):
```bash
ffmpeg -i clip.mp4 -vf "rgbashift=rh=-1:bh=1,\
noise=c0s=10:c0f=t+u:c1s=3:c1f=t+u:c2s=3:c2f=t+u,\
vignette=angle=PI/6:mode=forward,\
eq=saturation=1.02:contrast=1.02" \
-c:v libx264 -tune grain -crf 20 -preset slow -pix_fmt yuv420p out.mp4
```
- `rgbashift rh=-1:bh=1` = sub-pixel CA (1px, NOT the 6px VHS value). `noise` = luma-heavy sensor grain. `vignette PI/6` = barely-there falloff. `-tune grain -crf 20` preserves grain through the single recompression that itself reads as "phone export."
- **Soften/skip `rgbashift`+`noise` on product-hero/label frames** so the label stays clean. Apply once, after concat, to avoid stacking generational artifacts.
- **Do NOT** add synthetic crop-shake/deshake (reads vintage/glitch; real phones are optically stabilized; we already prompt natural handheld sway).

---

## 5. Negative / Constraint Strategy

**There is genuinely NO `negative_prompt` field** (verified against `_build_body`, `app/video_gen.py:297–320`; confirmed by [fal schema](https://fal.ai/models/bytedance/seedance-2.0/image-to-video/api)). Also no `fps`, `motion_strength`, `cfg`, `style`. All constraints are inline.

**Rules:**
1. **Lead affirmative.** Describe the texture you WANT (`visible pores, unretouched`), not the failure you fear.
2. **Never negate the subject noun.** Inline `no plastic skin` / `no blur` is parsed as positive text and can **introduce** the artifact (the documented "no subtitles → subtitles" failure mode). ([seedance2.so](https://seedance2.so/blog/seedance-2-prompts-guide))
3. **The ONE safe negation:** `no 3D, no cartoon, no VFX` — the only anti-CGI triad with verbatim sourcing (Higgsfield/imagine.art). It reads as a style token, not a hard exclusion.
4. **Content bans OK** (object-absence, not failure-nouns): `no on-screen text, no logo`. Add `no music` on the **silent i2v path only**, never on r2v.
5. **Keep the tail short** — the real rule is attention-decay after ~8 instructions, not a hard "3–5 bans" cap (that figure is Kling lore, not Seedance).

---

## 6. Parameter Table

| Key | Sent as | Our default | Recommendation |
|-----|---------|-------------|----------------|
| `model` | `seedance-2.0-image-to-video` / `-fast-` / r2v | standard for v2 | **Fast/720p for drafts + seed search; standard for finals.** r2v is the ONLY model accepting `audio_urls` (i2v 400s on audio). |
| `quality` (=resolution) | `"720p"` | `DEFAULT_CLIP_RESOLUTION="720p"` (`config.py:61`) | Keep 720p for iteration; flip to `"1080p"` **on standard model only** for hero finals, AND only after levers 1–6. Fast caps at 720p. 1080p alone does NOT fix plastic. |
| `duration` | int 4–15 | clamped, beats ≤5s | Keep 4–6s (the documented sweet spot). Integers only. |
| `seed` | int | `_seed_for()` = `sha256(workdir)[:8]+i` (`pipeline.py:122`) | Stable & reproducible-across-processes — **use it as the A/B instrument** (Section 7). Note: seed is *best-effort*, not bit-deterministic ([fal](https://fal.ai/learn/tools/how-to-use-seedance-2-0)). |
| `camera_fixed` | only sent when `True` (`video_gen.py:314`) | `True` for product/static b-roll, `False` for talking heads | Keep. Note `camera_fixed` is NOT in the fal 2.0 schema — the in-prompt "locked-off tripod" phrase is the real lever; don't depend on the boolean being honored. |
| `generate_audio` | bool | per route | Silent i2v for sync.so; r2v owns audio. Don't conflict `generate_audio` with `audio_urls` on r2v. |

---

## 7. A/B Test Plan

**Discipline:** pin the seed, change exactly ONE variable per run, run on **fast/720p** (the iteration loop can't exceed 720p anyway). Because the seed is best-effort (same seed "varies slightly"), use a **fixed trio of seeds** (e.g. beats 0/1/2 from `_seed_for`) and compare trios, not single pairs — otherwise seed-level waxiness noise masquerades as a "win." Hold every other payload key byte-identical.

Ordered by expected leverage (most likely win first):

| Run | Variable changed | What it isolates | Predicted |
|-----|-----------------|------------------|-----------|
| **0 — Baseline** | none (current live prompt + current anchor) | reference point | plastic (as reported) |
| **1** | **Texture/optics tail ONLY** added to `_talking_motion_prompt` (Section 3a) | the verified zero-cost gap | **expected biggest, cheapest win** |
| **2** | **+ Faithfulness + fixed lighting clause** | anti-drift + anti-shimmer | stabilizes texture across frames |
| **3** | **+ Post-process ffmpeg pass** (Section 4) | the missing capture-realism layer | masks residual smooth artifacts |
| **4** | **De-slicked anchor ONLY** (same prompt as Run 1) | the folklore "dominant lever" claim | if it visibly beats Run 1 across the trio, the anchor hypothesis holds and you fix the image stage |
| **5** | **720p → 1080p** (standard model) on the winning combo | sharpness only | lets won texture survive; NOT a fix on its own |

Judge plasticity on the **still first**, then the clip. If de-slick (Run 4) does NOT beat the prompt tail (Run 1), the motion prompt was the bottleneck — ship that and skip the anchor surgery.

---

## 8. Folklore / Does NOT Work (don't cargo-cult)

| Technique | Verdict | Why |
|-----------|---------|-----|
| `no plastic skin` / `no waxy texture` / `no beauty filter` negations | **Refuted** — invented & falsely attributed; backfire-prone | No negative_prompt field; inline negation of the subject noun can *introduce* the artifact. Use positive texture cues instead. |
| **chromatic aberration / rolling shutter** tokens | **Refuted for our route** | i2v locks optics from the anchor (tokens ignored/overridden); CA dirties high-contrast edges; rolling shutter needs motion to manifest and only risks invented wobble on a static face. ([studiobinder](https://www.studiobinder.com/blog/what-is-rolling-stutter/), [adorama CA](https://www.adorama.com/alc/chromatic-aberration/)) |
| **halation / focus breathing** | **Folklore for daylight UGC** | i2v can misread halation as moving smoke/fire; warm light turns it pink; focus breathing needs a focus pull our fixed framing lacks. "Soft highlight rolloff" gives the benefit without the misfire. ([hailuo](https://hailuoai.video/pages/knowledge/prompting-film-halation-retro-glow-guide)) |
| **ARRI Alexa / 35mm film-stock** on talking heads | **Wrong aesthetic** | Contradicts the iPhone-selfie look and invites a cinematic regrade that fights "preserve colors." |
| **`8K, ultra-sharp, hyperdetailed, masterpiece, cinematic`** quality-stacking | **Counterproductive** | Pushes toward CGI/"generic digital bloom." Already absent from our live path (grep-confirmed) — keep it that way. ([apiyi](https://help.apiyi.com/en/seedance-2-0-prompt-guide-video-generation-camera-style-tips-en.html)) |
| **`smooth/clean/perfect/flawless`** on skin | **Feeds the plastic prior** | Clinical cleanliness signals digital artificiality. "smooth" OK for camera motion only. |
| **Pre-baking film grain into the still** | **Refuted** | Frozen/smoothed AND suppresses motion ([arXiv 2506.08456](https://arxiv.org/html/2506.08456v1)). Grain → output video only. |
| **Bumping resolution as THE fix** | **Refuted by the user's own 1080p test** | Sharpens the texture floor; cannot create texture the anchor/prompt never encoded. |
| **"De-slick the anchor" as proven fact** | **Mixed — A/B-gate it** | Mechanism is unverified practitioner folklore; the "magichour confirms" citation is false (it says the opposite). Direction has 2-source support, so test, don't assume. |
| **Letting hands gesture in/out of frame** | **Mixed — A/B-gate, and reword** | Frozen hands reading robotic is real, but "enter and leave frame" fights our `_ANATOMY_CLAUSE` ("fully inside the frame") and crossing the edge triggers hand regeneration. If tested, use "one small slow gesture that stays fully in frame." NOT for product b-roll (second-hand creep). This is a naturalness lever, NOT a fix for the texture complaint. |

---

## 9. What to Change in the Code RIGHT NOW

**Priority order (ship 1–4 first; they target the verified gap):**

1. **`app/pipeline.py`** — add the three new constants `_SKIN_TEXTURE_CLAUSE`, `_FAITHFULNESS_CLAUSE`, `_ANTI_CGI_TAIL` (Section 3, near `_ANATOMY_CLAUSE` ~L322).
2. **`app/pipeline.py:403–411`** (`_talking_motion_prompt`) — change lighting line to "even soft diffuse daylight … steady constant exposure"; insert `{_SKIN_TEXTURE_CLAUSE} {_FAITHFULNESS_CLAUSE}` before `_ANATOMY_CLAUSE`; append `{_ANTI_CGI_TAIL}`. **Verify word count ≤100.**
3. **`app/pipeline.py:431–438`** (`_native_talking_prompt`) — same texture + faithfulness + anti-CGI inserts; lighting tweak; **no `no music`**.
4. **`app/pipeline.py:450–463`** (`_broll_motion_prompt`) — branch `texture`: full clause for lifestyle, "realistic material reflections" for product/static; add `{_FAITHFULNESS_CLAUSE}`.
5. **`app/ui.py`** (anchor prompts) — add the **positive** micro-texture cues (Section 3d); reword "no fake textures/imperfections" → "no plastic-CGI texture."
6. **`app/assembly.py`** — add the optional post-generation ffmpeg "phone capture" pass (Section 4), applied once after stitch/lipsync; soften it on product/label clips.
7. **`app/image_gen.py` `_config_for_tier`** — confirm the hero anchor renders at Nano Banana Pro's **max `image_size`**.
8. **`app/config.py:61`** — leave `DEFAULT_CLIP_RESOLUTION="720p"`; add a hero-finals path that sets `quality="1080p"` on the standard model only — but only after 1–6 land.

**Do nothing on:** `_build_body` (schema-correct, no keys to add); `camera_fixed`/`seed`/`duration` logic (already right); chromatic-aberration/rolling-shutter/halation/ARRI/8K tokens (folklore — never add).

**One-line summary:** the cheapest, highest-confidence, *verified* fix is wiring the already-written-but-unshipped skin-texture + faithfulness tail into the three live prompt functions — do that first, A/B it with the pinned seed, and only then chase the anchor and post-process layers.
