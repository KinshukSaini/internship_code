# Killing the AI Sheen in Seedance 2.0 i2v — An In-Depth, Backed Guide for Our UGC Pipeline

This guide is specific to our EvoLink → Seedance 2.0 pipeline. The exact payload we send is built in `EvoLinkSeedanceGenerator._build_body` (`app/video_gen.py:297-321`); the live prompts come from `_talking_motion_prompt`, `_native_talking_prompt`, and `_broll_motion_prompt` in `app/pipeline.py:384-463`. Every recommendation below maps to a verified technique and to a concrete line in those files.

---

## 1. Why i2v output looks plastic vs. the Nano-Banana-Pro source still (root causes)

The anchor still is photoreal, but the clip looks waxy. There are five root causes, in order of impact:

1. **Seedance re-paints, it doesn't just animate.** i2v anchors the first frame to your image, but every subsequent frame is *generated*, and the generation's prior is biased toward the smooth, beauty-filtered "AI look." The texture fidelity of the still (pores, vellus hair, micro-shadow, oil sheen) is **not carried forward by default** — frame 2 onward is re-synthesized and regresses toward smooth. The fix is twofold: (a) explicitly ask for the texture to be *preserved* and (b) explicitly name the micro-texture so the prior doesn't smooth it (the skin-micro-texture technique: "natural skin texture, visible pores, subtle vellus hair, gentle subsurface scattering, unretouched"). Our live `_talking_motion_prompt` and `_native_talking_prompt` say *none* of this — this is gap #5 in the audit and the single largest cause of the waxy look.

2. **720p is too soft to hold skin detail.** We hard-default `resolution`/`quality` to `"720p"` (`config.py:61`, sent as the `quality` key at `video_gen.py:306`). Native 1080p is the cleanest realism win because no post-hoc upscale/recompression is involved, and real skin/texture detail is preserved (resolution-tiering technique). At 720p, pores and fine texture fall below the resolution floor and the frame reads synthetic *before* it even gets recompressed downstream. 1080p is **standard-tier only** on Seedance 2.0; the fast endpoint caps at 720p (verified, fal standard vs. fast schema).

3. **No sensor/lens/optics language at all.** Real phone footage carries grain, sensor noise, slight rolling-shutter, focus breathing, soft highlight rolloff, and faint compression artifacts. Clean AI renders have none of these — that absence is itself a tell. Our prompts mention "iPhone front camera" but give the model no actual optical behavior to imitate (gaps #3, #4). The anti-CGI/anti-sheen technique (Higgsfield) recommends exactly this: `subtle film grain, focus breathing, soft highlight rolloff, faint halation on highlights` plus the inline negation `no 3D, no cartoon, no VFX` to force ultra-realism on organic/skin subjects.

4. **Head-bob-only motion reads as a render, not a person.** `_SECTION_MOTION` (5 tone strings) + `_MICRO_GESTURES` (4 face-only gestures) plus the deliberate hand-freeze (`_ANATOMY_CLAUSE`) is the *entire* motion vocabulary. The skill md itself warns this reads robotic. Frozen hands + repeated face-only motion removes the natural gesturing that sells a real creator. A real person whose hands never enter frame and who only bobs their head looks uncanny regardless of skin quality.

5. **Slow-mo / over-smooth temporal default.** Seedance, like most i2v models, defaults toward smoothed, slightly-slowed motion unless told otherwise. Smooth interpolation is part of what reads as "polished/AI." We already counter this somewhat ("real-time speed for the full clip") — keep and strengthen it.

The mental model: **the still defines frame 1; the prompt defines what the *generator* does to frames 2–N.** If the prompt is silent on texture, optics, and grain, the generator fills that silence with its smooth prior. You are not over-describing the scene — you are defending the texture the still already earned.

---

## 2. The ranked realism levers (highest → lowest leverage)

1. **Native 1080p on the standard endpoint for finals** (resolution-tiering). Biggest single mechanical win; no prompt can recover detail that the resolution floor threw away. ~2.8x cost (~$0.68/s vs ~$0.24/s) so reserve for finals; draft/seed-search on fast 720p.
2. **Explicit skin micro-texture + anti-plastic phrasing in the talking-head prompt** (skin-micro-texture; anti-CGI). Directly attacks the waxy tell. Costs nothing.
3. **Sensor/lens/optics realism tail** — `subtle film grain, faint sensor noise, soft highlight rolloff, focus breathing; no 3D, no cartoon, no VFX` (anti-CGI/anti-sheen). The "real phone capture" texture layer.
4. **Lighting descriptor matched to the still** (lighting is the official "highest-impact single element"). For i2v, *match* the still's light ("keep consistent soft daylight"), never impose a conflicting look.
5. **One concrete motion + one camera move, separated into two clauses, real-time pacing** (separate-camera-from-subject; motion-verbs). Kills slow-mo and jitter, adds life.
6. **"Preserve composition and colors" first-frame faithfulness clause** (i2v faithfulness). Stops the re-paint drift.
7. **Affirmative inline constraints** (no negative-prompt field exists) — `avoid jitter, avoid bent limbs, avoid plastic waxy skin, no on-screen text, no logo, no music`.
8. **Pinned seed + one-variable iteration** to actually measure which lever helped (seed-pinning). We already pin a deterministic seed — use it for A/B.

---

## 3. Exact positive-prompt building blocks (copy-paste)

Seedance 2.0 has no negative-prompt field (verified, fal i2v schema: `prompt, image_url, end_image_url, resolution, duration, aspect_ratio, generate_audio, seed, end_user_id`). All constraints go **inline, at the end, phrased affirmatively** ("avoid X" / "no X"). Keep total to **60–100 words** for i2v, leaning short (official guide; the first 20–30 words carry the most weight). Order: motion → camera → texture/optics → faithfulness → constraints. **Do not re-describe the subject/scene** — the still carries it (i2v faithfulness).

### Reusable texture/optics tail (drop-in for ALL clip types)
```
Natural skin texture, visible pores and fine skin detail, subtle subsurface
scattering, unretouched; shot on a phone sensor with subtle film grain, faint
sensor noise, soft highlight rolloff and gentle focus breathing. Preserve the
composition, colors and lighting of the first frame. Photorealistic, realistic
physics, natural motion. Avoid jitter; no plastic or waxy skin, no beauty
filter, no 3D or cartoon look, no on-screen text, no logo, no music.
```
*(Backed by: skin-micro-texture; anti-CGI/anti-sheen `no 3D, no cartoon, no VFX`; i2v faithfulness `preserve composition and colors`; photoreal+physics stack; positive-only constraints; audio/content negatives. Note: drop "gentle focus breathing / film grain" from product b-roll if it dirties the label.)*

### Talking head — sync.so route (silent i2v, `_talking_motion_prompt`)
```
Filmed on an iPhone front camera, handheld with a slight natural sway, soft
daylight on the face. The creator talks to the lens the whole time, {tone},
with {micro} and one or two small natural hand gestures that enter and leave
frame; mouth clearly opening and closing mid-sentence, natural blinks. The
camera holds a loose handheld medium shot at real-time speed. Gestures begin
and resolve within the first {window_len} seconds.
[+ reusable texture/optics tail above, with: exactly one pair of hands, five
fingers each, no extra or phantom hands; face and body proportions identical
to the first frame; avoid bent limbs.]
```
Key changes vs. today: (a) **add the texture/optics tail** (currently absent — gaps #3/#5); (b) **allow constrained hands back in** ("one or two small natural hand gestures that enter and leave frame") instead of fully freezing them, because frozen hands read robotic (gap #8) — keep the anatomy guard so they don't multiply; (c) keep the real-time/continuous motion (counters slow-mo). Backed by: separate-camera-from-subject, motion-verbs (one qualified motion), skin-micro-texture, anti-CGI.

### Talking head — native r2v route (`_native_talking_prompt`)
```
Filmed on an iPhone front camera, handheld with a slight natural sway, soft
daylight on the face. The creator looks at the lens and says: "{line}" — {tone}.
She speaks the full line naturally at real-time pace with small natural hand
gestures; her voice and lip movements follow Audio 1.
[+ reusable texture/optics tail, minus the audio/content "no music" (r2v
generates the voice track) and minus the gesture-window budget — the reference
audio paces the take.]
```
Keep the `Audio 1` binding sentence and the quoted line (verified working in the gate probe). Add the texture/optics tail. **Do NOT send `no music`/`generate_audio` conflicts here** — r2v owns the audio.

### Product / hands b-roll (`_broll_motion_prompt`, static-camera types)
```
{broll_shot}. {visual_note}. The camera holds completely still on a locked-off
tripod. The grip stays constant and the product keeps its exact shape, colour
and label throughout — no morphing or warping. The product moves only as the
single deliberate action this shot calls for (a steady hold, one slow turn to
the label) — the cap/lid stays on, no spray. The action begins and completes
early, then the shot settles for the final second.
Natural surface texture and realistic material reflections, soft natural
light, photorealistic, realistic physics. Avoid jitter, avoid reflection
distortion; no plastic look, no on-screen text, no logo, no music.
```
Backed by: fixed-camera-via-prompt phrasing, i2v product-stability ("stable proportions, clean label, no warping"), realism-physics ("realistic material reflections"), product-realism. Note for product: prefer "realistic material reflections" over heavy "film grain" so the label stays clean (anti-CGI caveat: don't over-stack grain on product). **`avoid reflection distortion`** is the product-specific negation (official constraint table).

### Lifestyle / no-product b-roll (`_broll_motion_prompt`, non-static)
```
{broll_shot}. {visual_note}. Gentle handheld camera movement at a steady
real-time pace, phone-camera feel. The action begins and completes early, then
the shot settles for the final second.
[+ full reusable texture/optics tail with grain — this is where "real phone
footage" texture sells hardest.]
```

---

## 4. Negative-prompt strategy

**Seedance 2.0 exposes no `negative_prompt` field** on the fal/EvoLink i2v route (verified: fal GitHub `seedance-2.0-api` schema and fal API page; our `_build_body` has no such field and shouldn't add one). The official guide treats constraints as step 6 of the prompt formula — affirmative, inline, at the end.

Recommended inline constraint tail (the "negative prompt" equivalent), tuned per shot type:
- **Talking head:** `Avoid jitter, avoid bent limbs, avoid identity drift; no plastic or waxy skin, no beauty filter, no 3D or cartoon look, no extra or phantom hands, no on-screen text, no logo, no music.`
- **Product/hands b-roll:** `Avoid jitter, avoid reflection distortion, avoid warping; the product keeps its exact label and shape; no spray, no extra hand, no on-screen text, no logo, no music.`
- **Lifestyle b-roll:** `Avoid jitter; natural imperfections, no plastic look, no on-screen text, no logo, no music.`

Two notes: (1) These read as positive style tokens, not hard exclusions, so keep them short (3–5 bans) and place them last (anti-CGI technique; positive-only constraints). (2) The skill md's verbatim content tail `No music, no logo, no text on screen` is **never appended on live clips** (gap #11) — this is how Seedance is currently free to invent watermarks/soundtrack on i2v takes. Add it (minus "no music" on the r2v route).

---

## 5. Parameter settings (concrete)

| Param (our key) | Today | Recommended | Backing |
|---|---|---|---|
| `quality` (= resolution) | `"720p"` always | **`"1080p"` for finals** (standard model), `"720p"` for drafts | resolution-tiering; 1080p = standard-only |
| `model` | `seedance-2.0-image-to-video` always; FAST unused | Drafts/seed-search → `seedance-2.0-fast-image-to-video` (caps 720p); finals → standard i2v | resolution-tiering, seed-iteration workflow |
| `duration` | `max(4, min(15, ceil(window)))`, typ. 5–6 | Keep. Discrete integer 4–15 (not continuous, no fractions). Shorter single-motion takes read more natural | duration enum technique |
| `camera_fixed` | `True` for tripod talking heads + product/hands b-roll; key omitted otherwise; never on r2v | **Keep, but verify the EvoLink/Seedance-2.0 route actually honors it** — 2.0 removed the param on fal/WaveSpeed schemas. If unverified on EvoLink, rely on the in-prompt lock phrase ("camera holds completely still on a locked-off tripod") which we already send | camera_fixed-removed-in-2.0; fixed-via-prompt |
| `seed` | deterministic per-beat `sha256(workdir)+i` | Keep. Use it for true A/B: lock seed, change ONE prompt element per run | seed-pinning |
| `generate_audio` | `True` only for r2v; `False` for i2v + all b-roll | Keep exactly. `False` for sync.so talking heads (downstream lip-sync), `True` only where native SFX helps and no VO is layered | generate_audio default-true + our lip-sync pipeline |
| `aspect_ratio` | `"9:16"` hard-coded | Keep. Set via param, never in prompt text | multi-shot/aspect-ratio technique |
| fps / motion_strength / cfg / style | not sent | **Do not add** — none exist in Seedance 2.0. Control motion via prompt only | i2v exposed-param schema |
| `negative_prompt` | not sent | **Do not add** — no such field. Use inline affirmative constraints | positive-only constraints |

Workflow: draft + seed-search on fast/720p → lock the winning seed → re-run on standard/1080p for the deliverable.

---

## 6. The "describe only motion, preserve the photo" principle

i2v prompts should describe **only what changes** (motion + camera) and **explicitly protect what shouldn't** (composition, colors, lighting, texture, identity). The still already encodes subject, wardrobe, and scene — re-describing them makes Seedance "pop" props in or morph them (i2v faithfulness; our own memory: don't re-describe the image). Our talking-head functions correctly omit `visual_note`/wardrobe — keep that.

To stop the model re-painting the still:
1. **Include the literal faithfulness clause** `preserve the composition, colors and lighting of the first frame` (i2v faithfulness — the exact phrase the official guide recommends). We currently *don't* say this anywhere — add it.
2. **Match, don't impose, lighting** — say "keep consistent soft daylight," never bolt on "golden hour" over an indoor still (lighting technique i2v caveat).
3. **Defend texture explicitly** so the smooth prior can't erase it — "natural skin texture, visible pores, unretouched" (skin-micro-texture). This is the counter-intuitive part: the way to *preserve* the photo's realism is to *name* its texture in words, because frame 2 onward is regenerated.
4. **Keep the subject clause minimal** — "the creator," not a fresh appearance description. (We do this; the only addition is the texture/optics tail, which is about *render quality*, not re-describing identity.)

There's a genuine tension here (gap #9): omitting the scene strips lighting/environment grounding. Resolve it by adding *grounding-via-preservation* ("keep the first frame's soft daylight and background") rather than *grounding-via-redescription* ("a kitchen with a window behind her"). The former protects the still; the latter invites morph.

---

## 7. Pitfalls

- **Slow-mo default.** Seedance smooths/slows motion unless told "real-time pace." Keep our "real-time speed for the full clip" and add it to b-roll (motion-verbs).
- **Stacking camera + subject motion.** The #1 cause of jitter (separate-camera-from-subject). Never "the camera orbits as she gestures." Two clauses: subject motion, then *one* camera move. Our b-roll correctly enforces this; keep it.
- **Over-describing the scene** → prop-pop/morph (i2v faithfulness). Don't re-add wardrobe/setting to talking heads.
- **Over-stacking grain/halation** can dirty the image, especially on product labels (anti-CGI caveat). Use the full grain tail on lifestyle b-roll and talking heads; trim it on product hero shots.
- **The word "fast."** Bare/unqualified "fast" reliably induces jitter (motion-verbs; official guide). If something must move quickly, make only ONE element fast and qualify the rest.
- **`Shot 1:`/`Shot 2:` labels accidentally triggering cuts.** Unlabeled prompts default to a single continuous take (what we want for UGC). Never use shot labels in a single-beat clip (multi-shot technique).
- **Frozen hands reading robotic** (gap #8). Don't fully suppress hands; constrain them ("one or two small gestures inside the frame, five fingers, no extra hands") so you get life without warps.
- **Assuming `camera_fixed` works on the EvoLink 2.0 route.** It was removed from the 2.0 fal/WaveSpeed schemas. Our in-prompt lock phrase is the real lever; keep sending it and don't depend on the boolean being honored (camera_fixed-removed technique; our own code comment flags r2v acceptance as unverified).
- **Length creep.** Adding the texture/optics tail must not push the prompt past ~100 words (official 60–100). Trim the tone/micro-gesture strings if needed; the texture tail is higher-leverage than a second adjective on the head-tilt.

---

*Primary sources cited inline: official Seedance 2.0 prompt guide (help.apiyi.com), fal.ai how-to + i2v model page + fal-ai/seedance-2.0-api GitHub, invideo.io guide, higgsfield.ai prompting guide. Pipeline references: `app/video_gen.py:297-321`, `app/pipeline.py:322-463`, `app/config.py:61`.*