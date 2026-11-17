# Gemini Omni Flash — capability + prompting research (2026-07-29)

Research pass on Google's `gemini-omni-flash-preview` for the v2 UGC pipeline. Every
claim below is tagged:

- **[V]** verified by a live call in this session
- **[S]** authoritative from the generated SDK types (google-genai 2.14.0)
- **[D]** from official Google docs
- **[C]** community/third-party guides — treat as folklore until verified
- **[?]** unknown / untested

---

## 1. What it is

**[D]** Announced at Google I/O on 2026-05-19 as the conversational successor to Veo in
the Gemini app; API access opened 2026-06-30 in public preview. Google's own video docs
now name it the **recommended default** for video generation, with Veo 3.1 kept for
cinematic/long-form work. Google has not announced a Veo 4.

| Property | Value | Src |
|---|---|---|
| Model ID | `gemini-omni-flash-preview` | [V] |
| API | **Interactions API** — `client.interactions.create()`, *not* `generateContent` | [V] |
| Output | 720p, 24 fps, H.264 + AAC stereo 48 kHz | [V] |
| Duration | 3–10 s (10.0 s exactly when unspecified) | [V] |
| Aspect | `16:9` (default) or `9:16` — nothing else | [S] |
| Native audio | Yes — dialogue, ambience and music in the same call | [V] |
| Inputs | text, image, video, audio (multi-input simultaneously) | [D] |
| Price | ~$0.10 per second of output (~$1.00 per 10 s clip) | [D] |
| Latency | 152 s and 263 s wall for two concurrent 10 s clips | [V] |
| Rate limit | 10 req/min on the Veo surface; Omni-specific not confirmed | [?] |
| Watermark | SynthID **+ C2PA Content Credentials**, non-optional | [D] |

**Relevant to the 2026-08-02 EU AI-Act disclosure deadline:** Omni stamps both SynthID
and C2PA provenance automatically, so the disclosure obligation is partly satisfied at
generation time rather than in our own finishing pass.

---

## 2. Verified behaviour (two live probes)

Both probes: 9:16, `delivery: "uri"`, Hinglish dialogue in the prompt, saved to
`jobs/omni-probe/`.

**A — `text_to_video`, no reference image.** Invented a 19-y/o Indian creator at a
warm-lit bedroom desk from description alone. → 720×1280, 24 fps, **10.000 s**, AAC audio.
Skin texture reads as real, not plastic; hands correct (one pair, five fingers, natural
knuckles); handheld selfie sway present without jitter. Wardrobe drifted from the
described "grey graphic tee" to a navy tee.

**B — `image_to_video` from an existing anchor** (`jobs/workflows/v2-20260714-154239`
anchor-0). → identical specs. Held the anchor's identity well: grey tee, silver chain,
handheld mic, bedroom desk with books and lamp all preserved.

**Two findings that matter more than the pixels:**

1. **It animated a photoreal human face without complaint.** This is the exact wall
   Seedance 2.5 hit (see `docs/pipeline-audit-2026-07.md`), the reason Kling 3.0 was
   being stood up as provider #2.
2. **Duration was 10.0 s despite a `[0-8s]` timecode block in the prompt.** Prompt
   timecodes shape *pacing inside* the clip; they do not set its length. Length is a
   request field (§3) that these probes did not pass.

**Gotcha — the official download snippet is broken.** Docs poll
`client.files.get(name=files/<id>)`; the real `output_video.uri` is already a
`.../files/<id>:download?alt=media` endpoint, so that loop raises
`JSONDecodeError`. Both probes generated fine and then failed to save. Use:

```python
video_bytes = client.files.download(file=interaction.output_video.uri)
```

Persist `interaction.id` before any post-processing — `client.interactions.get(id)`
(positional arg, **not** `name=`) refetches a completed generation for free.

---

## 3. The real parameter surface

**[S]** From google-genai **2.14.0** generated types. The repo used to pin **2.6.0**,
which predates the model and cannot describe `video_config` at all; the floor is now
**>=2.14** (locked at 2.16.0, where `VideoConfigParam` still has exactly `task` and
`VideoResponseFormatParam` exactly `aspect_ratio/delivery/duration/gcs_uri/type`), so the
installed wheel and this table agree. `interactions.create` takes `(*, request=None,
**body)` — our keyword call style passes straight through as the request body.

### `client.interactions.create(...)`

| Param | Type | Notes |
|---|---|---|
| `model` | str | `gemini-omni-flash-preview` |
| `input` | str \| list of content parts | see content types below |
| `response_format` | dict | video output shape — see below |
| `generation_config` | dict | see below |
| `previous_interaction_id` | str | multi-turn conversational editing |
| `system_instruction` | str | **exists in SDK, docs say unsupported** [?] |
| `response_modalities` | list | `text` \| `image` \| `audio` \| `video` \| `document` |
| `service_tier` | enum | `flex` \| `standard` \| `priority` |
| `background` | bool | async; pair with `webhook_config` |
| `webhook_config` | dict | completion callback — no polling |
| `store` | bool | **`False` disables editing in later turns** |
| `stream` | bool | SSE events |
| `tools` / `agent` / `environment` | — | agentic surface, not needed here |

### `response_format`

```python
{"type": "video",
 "aspect_ratio": "9:16",   # 16:9 | 9:16 only
 "duration": "8s",         # free-form string, NO enum — format unverified [?]
 "delivery": "uri",        # inline | uri — use uri above ~4MB
 "gcs_uri": "gs://..."}    # Vertex only, required there for uri delivery
```

`duration` is the single most valuable untested field: it would let each clip match its
beat window instead of paying for a fixed 10 s. Its value format is not in the docs and
not constrained by the schema — `"8s"` follows Google convention, but confirm with one
short call before designing around it.

### `generation_config`

```python
{"video_config": {"task": "image_to_video"},   # ONLY field video_config has
 "seed": 777,                                  # exists; effect unverified [?]
 "speech_config": [{"language": "hi-IN",       # exists; effect unverified [?]
                    "voice": "...",            # no enum published
                    "speaker": "..."}],
 "thinking_level": ..., "max_output_tokens": ..., "stop_sequences": [...],
 "transcription_config": {...}, "tool_choice": ...}
```

**`video_config` has exactly one field, `task`** — a firm negative result. There is no
resolution, fps, negative-prompt or motion-strength knob. 720p/24fps are not choices.

`task` ∈ `text_to_video` | `image_to_video` | `reference_to_video` | `edit`. Omitting it
makes the model infer intent from the prompt and inputs.

**`seed` and `speech_config` are the two levers worth one paid test each.** `seed` is the
obvious answer to cross-clip drift; `speech_config.language`/`voice` is the only thing in
the whole surface that looks like voice-identity control, and it is conspicuously absent
from the docs' feature list *and* from its "unsupported" list.

**Schema/doc conflicts — report, don't guess.** Docs list system instructions, stop
sequences and negative prompts as unsupported, yet `system_instruction`, `stop_sequences`
and `seed` are all live fields. Conversely `temperature` and `top_p` were in 2.6.0's
`GenerationConfigParam` and are **gone** in 2.14.0 — there the schema corroborates the
docs. So: no temperature, no top_p, no negative-prompt field.

### Input content parts

```python
{"type": "text",  "text": "..."}
{"type": "image", "data": <base64>, "mime_type": "image/jpeg"}
{"type": "document", "uri": file_uri}                    # Files API upload
{"type": "video", "data": <base64>, "mime_type": "video/mp4"}
```

---

## 4. Prompting techniques

### 4.1 Six-dimension frame **[C]**
Cover all six in one prompt: **shot framing & motion · style · lighting · location ·
action · text rendering.** A prompt is a creative brief, not a caption.

### 4.2 Lock the single take — non-negotiable for UGC **[D]**
**By default Omni cuts its own mini-narrative across several shots.** For one clean
selfie take you must say so: *"in a single unbroken scene"*, *"in a single continuous
shot"*, *"no scene cuts"*. This is the highest-leverage sentence in any UGC prompt.

### 4.3 Audio is prompted, not attached **[D]**
Audio generates by default; steer it in words. Put the spoken line in quotes and
describe the voice around it (age, timbre, pace, accent). Suppress with inline
negatives: *"no dialogue"*, *"no background music"*, *"no extra sound effects"*.
Distinguish diegetic (*"the click of her heels, no music"*) from scored **[C]**.

### 4.4 Timecode blocks **[D]**
Natural (*"after 3 seconds, she enters"*) or block form:

```
[0-3s] A person is walking
[3-6s] They stop and turn around
[6-10s] They start running
```
Verified: these direct pacing **within** the clip, not its length.

### 4.5 Image role tags **[D]**
```
<FIRST_FRAME> a woman is walking in the style of <IMAGE_REF_0>
```
`<FIRST_FRAME>` = starting frame; `<IMAGE_REF_N>` = subject reference, **zero-indexed**.
Explicit form for multi-image:
```
[# Sources <FIRST_FRAME>@Image1] [# References <IMAGE_REF_0>@Image2]
a woman <IMAGE_REF_0> is walking. Use Image1 as the starting frame.
```
This maps cleanly onto our existing anchor + product-ref pairs.

### 4.6 Conversational editing **[D]**
Chain with `previous_interaction_id`. Keep edits **simple and one change at a time** —
complex re-descriptions cause collateral drift. Always append **"Keep everything else
the same."** Numbered multi-edits work **[C]**. `store=False` forfeits this entirely.

### 4.7 Meta prompting **[D]**
Instruct on quality principles, not just content: *"Consider micro-detail, expression
and timing to create a very rich, detailed but entirely natural scene."*

### 4.8 Camera vocabulary is command-grade **[C]**
`oner`, `locked off`, `push in`, `pull out`, `dolly zoom`, `orbit`, `tilt`, `pan`,
`handheld`, `webcam style`. Even `static` must be stated.

### 4.9 In-video text **[D]**
Renders legibly; name the string exactly (*"a street sign that says: 'X'"*). Roughly one
word per second. Directly usable for CTA/end cards.

### 4.10 Lean on world knowledge **[D]**
It has Gemini's factual grounding — don't over-specify well-known things.

### 4.11 Latency knobs **[D]**
Fastest synchronous path: `background=False, store=False, stream=False` — but that trades
away conversational editing. `delivery="uri"` above ~4 MB.

---

## 4b. Audio as INPUT — can we drive it with our own VO?

**Short answer: no.** This is the question that decides the integration, so the evidence
in detail:

- **[S]** The Interactions API *does* define an audio input block:
  `{"type": "audio", "data" | "uri", "mime_type": ..., "sample_rate": ..., "channels": ...}`
  accepting `audio/wav`, `mp3`, `aac`, `flac`, `opus`, `m4a`, `l16`, `aiff`, `ogg`,
  `alaw`, `mulaw`. So audio can be *attached* without a schema error.
- **[D]** Omni's limitations list, **verbatim**: *"Uploading audio references is
  unsupported in the current version of the API."* and *"Voice editing is not
  supported."*
- **[D]** The official page contains **zero code samples** passing audio as input. Every
  example uses `text`, `image`, `document` (for a video URI), or raw video base64. There
  is no supported-input-format table; the only mime types shown anywhere are
  `image/jpeg`, `image/png`, `video/mp4`.
- **[D]** Lipsync and audio-driven timing are **not mentioned anywhere** on the page.
  Timing is prompt-driven only — natural language (*"after 3 seconds…"*) or timecode
  syntax.
- **[D]** The entire audio *prompting* section is about describing the audio you want
  **generated**: *"By default the model will try to generate an appropriate audio track
  for a video. This might not always be what you want. You can use your prompt to
  describe the type of audio you want."*
- **[D]** The model reference page lists input modalities as **Text, Image, Video** —
  audio absent — even though the overview page and all marketing say "any-to-any,
  accepts text/image/audio/video."
- **[C]** Every third-party guide uses audio only as a **rhythm/mood reference**
  ("use the audio as the rhythm and mood reference, match the visual transitions to the
  main beat") — music-driven pacing, never a speech track.

`AudioContentParam` is a **generic** Interactions API content type shared by all Gemini
models, which ingest audio for understanding/transcription. Its existence is not evidence
that Omni's *video generator* consumes it.

**Consequence:** there is no "lipsync to this WAV" mode. Omni speaks only in a voice it
invents, at a pace it chooses. Our ElevenLabs clone cannot drive it directly. Note the
hedge in Google's own wording — *"in the current version of the API"* — so this is
plausibly a roadmap item, not a permanent architectural limit. Re-check on each release.

### The workaround: silent Omni + our existing post-hoc lipsync

We do not actually need audio input, because we already own the piece it would replace.

```
Omni (i2v from anchor, prompted "no dialogue, no music", gentle closed mouth)
        ↓  silent clip, real face, real skin texture
ElevenLabs cloned VO  →  isolated per-beat audio  →  syncso / heygen-replicate
        ↓
mouth-only replacement in our cloned voice
```

This is **exactly the shipped architecture** from
[[heygen-replicate-lipsync-2026-06]] / [[lipsync-architecture-2026-06]], with Omni
swapped in for Seedance/Kling at the i2v step. It keeps the VO as master clock, keeps
forced alignment, keeps beat windows, keeps captions from our alignment data, and keeps
the voice clone. Omni contributes only what it is uniquely good at here: **animating a
photoreal face without refusing** [V], and better skin texture than Seedance.

Two requirements this places on the prompt and the request:

1. **Prompt for a gentle/quiet source mouth**, not a talking one — the same "gentle source
   mouth" tactic already shipped for syncso — plus explicit *"no dialogue, no background
   music, no extra sound effects"* so we get a clean silent (or discardable) track.
2. **`duration` becomes load-bearing.** Beat windows vary; a fixed 10 s clip per beat is
   the [[evolink-spend-drivers-2026-07]] 4s-floor waste problem all over again, at $1.00
   per clip. Verifying the `duration` string format is the single highest-value paid test.

---

## 5. Documented limits and dead ends **[D]**

- No **video extension**, no **frame interpolation** — so no first+last-frame control
  (Veo 3.1 has both; Omni does not).
- No **voice editing**; **audio references unsupported** → no voice cloning path.
- Video references **≤3 s** accepted by schema but not processed correctly; multi-video
  reasoning unsupported; YouTube sources unsupported.
- **English fully supported; other languages unevaluated** — Hinglish is off-spec.
- No provisioned throughput. Editing *uploaded* video blocked in EEA/CH/UK (editing
  model-generated video is fine); some recognizable people cannot be edited.
- **[C]** Changing audio requires full regeneration — no isolated audio edit.

---

## 5b. The UGC prompting playbook

### ⚠️ Do not port our Seedance prompts. The style is inverted.

This is the single biggest prompting finding. `docs/seedance-prompting-guide.md` §6
establishes our house rule — *"describe only motion, preserve the photo"*, keep the
subject clause minimal ("the creator," never a fresh appearance description), because
re-describing the still makes Seedance pop props in and morph them.

**Omni wants the opposite.** Every source converges on: describe the **whole scene**
across all six dimensions, narratively, and the model rewards *more* detail
(*"Be extremely detailed in your descriptions of characters and environments"* — official
meta-prompting advice). Keyword lists actively underperform: **[C]** *"keyword dumping
signals to the model that you lack clear intent."*

Reason for the difference: Seedance i2v is a weak-conditioning animator that drifts off
its first frame, so our prompts are **defensive**. Omni is a reasoning model with world
knowledge that treats the prompt as a **creative brief**. Feeding Omni our terse
motion-only prompts wastes most of its capability; feeding Seedance an Omni-style prompt
causes morph.

**What DOES carry over from our house style:** real-time pace, separating camera motion
from subject motion (one camera move per clip), no `Shot 1:`/`Shot 2:` labels, hand
constraints rather than hand suppression, and the flattering-skin-texture framing from
[[influencer-skin-texture-flattering]] — never pores/oil emphasis.

### The six dimensions **[C]**, ordered as a prompt

**shot framing & motion · style · lighting · location · action · text rendering**

Google's own 5-element framing is a superset worth following for briefs **[C]**:
**Goal** (declare the format: "a vertical social video") → **Input role** (what each
uploaded asset *is*) → **Scene** (~20 words) → **Motion** (camera + subject) →
**Constraints** (what must stay constant; explicit exclusions).

### The ranked levers for UGC specifically

1. **Lock the single take.** *"in a single unbroken scene / no scene cuts."* Omni's
   default is to cut its own multi-shot mini-narrative. Highest-leverage sentence there is.
2. **State the input role explicitly.** Not just an attached image — *"Use the provided
   image as a character reference. Preserve facial features, clothing and coloring
   throughout."* **[C]** *"One well-chosen character reference usually outperforms
   multiple generic ones."*
3. **Direct the audio in words, or it invents its own.** Spoken line in quotes, voice
   described around it (age, timbre, pace, accent). For a talking head you almost always
   want *"no background music, no extra sound effects"* so speech dominates.
4. **Physics/material language is the realism lever** **[C]** — the equivalent of our
   texture tail. *"fabric moves with natural draping and wind response, not stiff"*;
   *"soft diffused light, slight skin texture visible, no smoothing"*; *"natural head
   movement, subtle micro-expressions, realistic blink rate."*
5. **Camera vocabulary as commands** **[D]**: `oner`, `locked off`, `static`, `push in`,
   `punch in`, `pan left`, `dolly zoom`, `orbit`, plus capture-style tokens that matter a
   lot for UGC — **`natural smartphone zoom`, `grainy webcam style`, `vintage film
   camera`**. Even `static` must be said.
6. **Timecode blocks** to shape beats inside the clip (not its length):
   `[0-3s] … [3-6s] … [6-10s] …`
7. **Meta-prompt the quality bar**: *"Consider micro-detail, expression and timing to
   create a very rich, detailed but entirely natural scene."*
8. **Inline exclusions** — there is no negative-prompt field, so constraints ride the
   positive prompt at the end: *"no dialogue / no scene cuts / no on-screen text / do not
   change the character's face."*
9. **Social-first opening** **[C]**: *"start with a visually strong first second"* — real
   for scroll-stop, and it pairs with the Hook Variant Factory idea.
10. **On-screen text** is legible if quoted exactly, ~one word per second, English most
    reliable — usable for CTA end cards.

### Drop-in template for our talking-head beat

```
Create a vertical 9:16 UGC social video. Use the provided image as a character
reference — preserve the face, hairstyle, wardrobe, jewellery and room exactly.

In a single unbroken continuous shot, no scene cuts: <the creator> sits at <location
from the anchor>, holding the phone at arm's length in a handheld selfie framing,
talking straight to the lens with <energy> energy. Natural smartphone capture, loose
handheld sway, no jitter, real-time pace.

[0-3s] leans slightly toward the lens as he starts talking, one quick eyebrow raise
[3-6s] one small gesture at chest height, smirk lands and settles
[6-8s] settles back, holding eye contact

Audio: he says out loud, in a <age/timbre/pace> <accent> voice: "<the beat line>".
No background music, no extra sound effects.

Soft diffused light, clear healthy attractive skin with fine natural texture and a
matte non-shiny finish, no smoothing, shallow depth of field with a natural room
background. Natural head movement, subtle micro-expressions, realistic blink rate.
Consider micro-detail, expression and timing to create a very rich, detailed but
entirely natural scene.

Exactly one pair of hands, five fingers each, gestures inside frame. Do not change
the character's face or outfit. No on-screen text, no logo, no captions.
```

### Multi-turn strategy for consistency **[C]**

Rather than one mega-prompt: (1) base shot — subject + scene + motion, (2) **lock
identity** — *"keep the same face, same outfit, same colour palette"*, (3) refine in
layers, one category per turn (lighting, then camera, then action, then audio), (4) final
polish — *"a slightly cinematic feel with light 35mm grain."* Every edit turn ends with
**"Keep everything else the same."** Numbered multi-edits work: *"1. Change the jacket to
burgundy. 2. Add rain on the window. Keep everything else identical."*

Costs to weigh: each turn is a full paid regeneration (no isolated audio edit **[C]**),
and `store=False` forfeits the whole mechanism.

### Failure modes, consolidated

| Symptom | Cause | Fix |
|---|---|---|
| Unwanted cuts / mini-montage | **default behaviour** | "single continuous shot, no scene cuts" |
| Random music or wrong ambience | no audio direction given | always state music/sound/silence |
| Edit changes things you didn't ask | edit prompt too complex | one change + "keep everything else the same" |
| Generic, stock-looking output | underspecified (subject+action only) | add setting, lighting, camera, audio |
| Stylised/floaty motion | no physics language | name material/gravity/draping behaviour |
| Wardrobe drift **[V]** | described in text, not referenced | supply a character reference image |
| Messy on-screen text | unquoted or too long | short, quoted, English |
| Video reference ignored | >3 s refs not processed **[D]** | keep refs ≤3 s |

---

## 6. What this means for our pipeline

Two genuinely different ways in, and they are not close on risk.

### Option A — Omni as a silent b-roll / i2v provider (low risk)
Drop-in third `BROLL_VIDEO_PROVIDER` beside `seedance` and `kling`, prompted with *"no
dialogue, no music"*. Keeps the ElevenLabs → forced-alignment → per-beat lipsync spine
untouched. Buys us: a provider that **does not refuse photoreal faces** [V], plus much
better skin texture than Seedance. Costs: a second API shape in `video_gen.py` (Omni is
not on the EvoLink surface), 720p vs Kling's 1080p, and $1.00 per 10 s vs kling-v3's
~5.4 credits/s.

### Option B — Omni as a full talking-head generator (high risk, high reward)
Its native audio collapses three stages — Seedance i2v + ElevenLabs VO + syncso/HeyGen
lipsync — into one call, and lipsync is inherently perfect because the model authors both
tracks. That directly attacks the sync-3 articulation problem in
`docs/syncso-articulation-audit-2026-07.md`.

But it breaks the pipeline's foundational assumption. Our architecture makes the
**voiceover the master clock** — VO first, forced alignment, beat windows, then visuals
cut to it. Omni decides its own timing internally and exposes no alignment, so that clock
disappears. Concretely at risk:

1. **Voice identity across clips.** No cloning, no documented voice selection; a 31 s
   script needs ≥4 clips. If timbre drifts between them the video is unusable. `seed` and
   `speech_config` are the only hopes and both are unverified.
2. **Hinglish is explicitly unevaluated**, and it is our house register.
3. **The voice library / clone work does not transfer** at all.
4. **720p** is below the 1080p we settled on for talking heads.
5. Captions would need ASR off the generated audio instead of our alignment data.

### Recommendation
Ship **A** first — it is additive, reversible, and immediately solves the
Seedance-blocks-faces problem. Evaluate **B** behind a flag, gated on three cheap
questions, in one batched run:

1. Does `duration: "8s"` work, and what format does it want?
2. Does `seed` hold face **and voice** stable across two calls with one prompt?
3. Does `speech_config` change anything — and does Hinglish survive it?

Until #2 answers yes, B cannot produce a multi-clip video with a consistent creator.

---

## 7. Integration outcome (2026-07-29, shipped)

The user chose **full end-to-end Omni mode** (Option B, not the silent-b-roll
Option A) with prompted voice instead of TTS. Shipped as a new top-level
`video_generator ∈ ("seedance", "omni")` axis:

- **No TTS at all in omni mode.** Synthetic word-budget clock
  (`_omni_windows`, OMNI_WORDS_PER_SECOND=2.3), every window clamped to the
  API's [3,10]s; real timing recovered per clip via speech-end trim; captions
  force-aligned against the finished video (ElevenLabs = analysis only,
  optional — runs and resumes both survive a blank key).
- **`OmniVideoClient`** (video_gen.py): Interactions API, `background=True` so
  the interaction id persists before polling (crash recovery), duration-400
  retry-without, bytes → R2 re-upload so `VideoResult.video_url` stays durable.
- **Opus writes the prompts** with its own cached skill block
  (`video-omni-skill.md`); f-string builders (`_omni_talking_prompt` etc.) are
  the fallback; `_omni_safety_tail` re-appends verbatim tokens (single-shot
  lock, anatomy guard, music ban, persona clause) if paraphrased away; the
  decapsed/respelled spoken line is passed explicitly and its absence forces
  the template fallback (a talking clip without its line is a dead paid clip).
- **Voice consistency bet**: run-level seed (`_seed_for(work, 0)` on every
  clip) + byte-identical persona clause (`render_voice_persona(...,
  include_pace=True)`) in every speaking/VO prompt. UNVERIFIED until the first
  paid multi-clip run.
- Assembly rides `native_lipsync=True` unchanged (audio already normalized to
  aac/44.1k in every section-1 path); UI radio on the v2 tab gates the
  ignored widgets; resume round-trips `params.video_generator` and re-orders
  omni clips with the run-level seed.

Full decision log + review findings: `tasks/todo.md` (2026-07-29 section).

## Sources

- [Video generation in the Gemini API](https://ai.google.dev/gemini-api/docs/video)
- [Generate and edit videos with Gemini Omni Flash](https://ai.google.dev/gemini-api/docs/omni)
- [Gemini Omni Flash model reference](https://ai.google.dev/gemini-api/docs/models/gemini-omni-flash)
- [Gemini Omni Flash Preview — Google Cloud](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/omni-flash-preview)
- [Google Omni Prompting Guide — Promptslove](https://promptslove.com/blog/google-omni-prompting-guide/)
- [Gemini Omni Flash: API, Pricing & Limits — Coursiv](https://coursiv.io/blog/gemini-omni-flash)
- google-genai 2.14.0 generated types (`google/genai/_gaos/types/interactions/`)
