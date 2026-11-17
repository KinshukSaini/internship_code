# UGC Format Library

The reference for every selectable format in the timeline planner
(`app/gemini.py` → `plan_timeline`). The planner chooses these per-video and
per-beat from the brief; you can also force `hook_type` and
`narrative_structure` at request time (`generate_ugc_video(..., hook_type=,
narrative_structure=)`).

## The fixed ad anatomy (always enforced)

Every video follows this structure; each beat is tagged with its `ad_section`
and the planner is validated to keep them in order (`_validate_timeline`):

| Section | Timing | Purpose |
|---|---|---|
| **hook** | 0-3s | One pattern-interrupt line that stops the scroll |
| **problem** | 3-10s | Name the pain / context the viewer recognises |
| **solution** | 10-20s | The product / demo — pays off the hook (lean on B-roll) |
| **proof** | (within body) | Before/after, testimonial, social proof — "show don't claim" |
| **cta** | last 3-5s | EXACTLY ONE call to action |

Rules: beats ordered hook→problem→solution→proof→cta; exactly one CTA run at
the end; visual reset every 3-5s; one CTA only; **dual-coded** (spoken words
always captioned, plus short `text_overlay` emphasis on hook / key claim /
CTA).

## `hook_type` — the opener style (first 0-3s)

| Value | What it means | Example |
|---|---|---|
| `pov` | Puts the viewer inside the scenario with "you" | "POV: you've tried every acne cream and still break out" |
| `stop_scrolling` | Direct address + loss aversion | "Stop scrolling if your skincare isn't working" |
| `controversial` | A claim that forces attention | "This might be controversial, but serums are a scam — except one" |
| `curiosity_gap` | Opens an info loop the brain must close | "Nobody tells you that your moisturiser is the problem" |
| `question` | A provocative question | "Why does my skin break out every single week?" |
| `listicle` | Commits the viewer to a payoff count | "3 reasons I threw out my old serum" |
| `result_first` | Leads with the outcome | "This faded my dark spots in 3 weeks" |
| `callout` | Names the target audience | "If you're a busy mom with no time for skincare…" |
| `myth_buster` | Reverses a belief | "I thought this was a scam until I actually used it" |

## `narrative_structure` — the overall arc

| Value | What it means |
|---|---|
| `pas` | Problem → Agitate → Solution → CTA (the workhorse) |
| `storytime` | First-person mini-story; the product is the turning point |
| `day_in_life` | Casual routine vlog with the product woven in |
| `testimonial` | Genuine review / personal recommendation |
| `comparison` | This vs that (old solution vs the product) |
| `listicle` | Numbered tips / reasons |
| `demo` | How-to / tutorial focused |

## `framing` — talking-head camera formats (per beat)

The creator's face is on camera and speaking (this beat gets lip-synced).

| Value | What it means | Best for |
|---|---|---|
| `selfie` | Handheld front-camera at arm's length, casual | Default confessional, hooks |
| `lav_mic` | Handheld wireless mic near the mouth — **the desi creator signature**, news-reporter credibility | Opinion hooks, reviews, "let me tell you" |
| `tripod` | Propped static medium shot, hands free | Demos, steady talking |
| `walking` | Walk-and-talk, handheld, movement energy | Day-in-life, narrative bridges |
| `approach` | Walks toward the lens, frame goes wide→tight, max energy | 0-3s energetic hooks |
| `confessional` | Very tight intimate close-up, talking to a friend | Emotional beats, vulnerable story |
| `mirror` | Phone held to a mirror, reflection visible (phone in hand) | Outfit/result reveals |
| `grwm` | Get-ready-with-me — doing makeup/hair while talking | Beauty/skincare, incidental product placement |
| `punch_in` | Tight emphasis crop (as if a rapid zoom landed) on a key line | Punchlines, key claims |
| `product_reveal_pivot` | Brings the product into frame in a "let me show you" gesture | Hook→product transition |

## `broll_type` — cutaway shot categories (per beat)

A cutaway (no talking face) while the voiceover keeps playing under it. No
lipsync — these conveniently cover seams and add visual variety.

| Value | What it means |
|---|---|
| `product_closeup` | Macro of the product itself |
| `hands_using` | Hands applying / using / holding the product |
| `unboxing` | Opening the packaging |
| `before_after` | Split or sequential result comparison |
| `texture_pour` | Macro texture / swatch / pour / sizzle |
| `product_in_scene` | Styled product in an environment / flat-lay |
| `demo_steps` | Step-by-step usage shots |
| `lifestyle` | Environment / lifestyle context shot (no product) |

Each B-roll beat also has a free-text `broll_shot` for specifics (e.g. "macro
of the serum dropper with a drop forming").

## Voice library (picked by `describe_character`)

The character-selection step also picks a **`voice_vibe`** from the brief.
The pipeline resolves `(character.gender, voice_vibe) → voice_id` via the
curated catalog in `app/voice.py` (6 vibes × 2 genders, sourced from the
ElevenLabs shared library ranked by community clone-count).

| `voice_vibe` | Female | Male | Best for |
|---|---|---|---|
| `upbeat_lively` | Anika — Sweet and Lively | Bunty — Energetic, Expressive and Fun | Default UGC reels, energetic |
| `warm_relatable` | Sia — Approachable Content Creator | Manav — Friendly, Calm and Natural | Story/testimonial/day-in-life |
| `sassy_expressive` | Noor — Expressive, Sassy and Humorous | Bunty (stand-in — no dedicated sassy male in catalog yet) | Humor / gossip / controversial hooks |
| `youthful_excited` | Monika Sogam — Youthful and Expressive | Bunty (high-energy stand-in) | Beauty / skincare / fashion punch |
| `professional_clear` | Sia — Sweet & Smart Sales Professional | Raj — Professional Indian English | Tech / finance / explainer |
| `soothing_narrative` | Simran — Cheerful Best Friend | Amit Gupta — Soothing and Conversational | Wellness / storytime / calm |

Override at any time by passing `voice_id` to `generate_ugc_video(...)` or
the Workflow v2 textbox. Swap a catalog slot by editing `VOICE_CATALOG` in
`app/voice.py` — the LLM keeps picking the same vibe, the voice underneath
changes deterministically.

## How a beat is rendered

- **talking_head**: `framing` → the camera/composition stack in
  `docs/character-image-skill.md` §3g → Nano Banana anchor image (identity-
  locked to the character ref) → Seedance single-image I2V → Sync.so lipsync.
- **broll**: `broll_type` + `broll_shot` → `write_broll_prompt` → Nano Banana
  anchor (product-locked) → Seedance single-image I2V → silent (VO over it).

`text_overlay`, when set, is the dual-coded emphasis text for that beat (the
word-level karaoke captions from `app/captions.py` run throughout regardless).
