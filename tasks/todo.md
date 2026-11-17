# TODO — Omni in-clip multi-shot (2026-08-05)

Replace "one clip = one shot, chop long beats into more clips" with Omni's own
native multi-shot: ONE generation carries 2-3 internal cuts while the dialogue
runs continuously over them. Officially supported via timecode blocks
(`[0-3s] … [3-6s] …`), and multi-shot is Omni's DEFAULT — which
`_OMNI_SINGLE_SHOT_CLAUSE` has been suppressing all along.

## When to use it (the grouping rule)

Per BEAT, never across beats — 1 beat = 1 clip stays intact, so assembly,
captions, manifest, resume and per-beat QC are all untouched.

| Beat | Treatment | Why |
|---|---|---|
| speech/VO beat estimating > `OMNI_SHOT_MAX_S` (5s) | ONE clip, internal cut every ~`OMNI_SHOT_TARGET_S` (3s) | kills the 6-10s hang without paying the 3s floor per chunk |
| beat ≤ 5s | single shot, unchanged | already inside the bar |
| estimate > `OMNI_DUR_MAX` (10s) | split into ≤10s clips first, each then cuts internally | 10s is a hard API cap on both routes |
| silent hold | single shot, capped at 5s | no dialogue to carry across a cut |
| `uses_product` | single shot, anchored | the product anchor IS the grounding; a model-invented product shot has no reference |
| hook (index 0) | single shot, anchored | first 3s decide the ad; keep it fully directed |

## Background fidelity (the hard requirement)

Every timecode segment is written FROM THE BEAT'S OWN SCREENPLAY, never left to
the model. Each names the same room/setting and wardrobe plus a reframe from a
fixed same-scene ladder (tighter on the face / hands and surface / back out to
the framing), and an anti-invention clause forbids any new location, set,
background, person or product. The cut is a CAMERA change inside the described
scene — not a new scene.

## Tasks

- [x] Research: multi-shot + timecode blocks + audio across cuts (docs confirm)
- [x] `OMNI_MULTISHOT` toggle + cut cadence constants
- [x] `_omni_windows`: stop splitting 5-10s beats into separate clips
- [x] `_OMNI_BG_LOCK_CLAUSE` + same-scene reframe ladder
- [x] `_omni_cut_plan()` → timecode blocks from the beat's screenplay
- [x] Wire into `_omni_talking_prompt` + `_omni_vo_broll_prompt`
- [x] Tests
- [ ] Live A/B on the real script — cut fidelity is UNVERIFIED until then

## Review — shipped, and what it exposed

Shot bar is now 2-3s per SHOT (`OMNI_SHOT_MAX_S=3.0`, target 2.5s) while a TAKE
may run the full 10s. Measured on the real 2026-08-05 script:

| | before today | after |
|---|---|---|
| longest shot on screen | **10.0s** | **3.0s** |
| clips | 15 | 29 (4 of them multi-shot takes) |
| video ordered | 76s (~$7.5) | 131s (~$11.40) |
| mid-sentence truncations | 3 (clips 0/4/7) | 0 (headroom) |

**The ceiling is fixed. The cost went the wrong way, and the fold barely fired —
both for one structural reason.** This script is 15 beats carrying ~76s of
speech: ~5s of dialogue per beat. A 2-3s shot bar therefore needs ~30 beats, and
downstream splitting cannot create them — dividing one beat yields shots that
share that beat's single scene (an angle change), never a new screenplay scene.
So most clips here are divided sub-shots, which by design refuse to fold (folding
them built a shot list that argued with its own "the scene must change" clause).

Only 4 folds happened, all between genuinely different adjacent beats.

## Issues found by the 2026-08-06 live run — status

- [x] Opus flattened both takes into single-shot prompts → Opus now RECEIVES the
      shot plan, the skill doc has a multi-shot exception, and its output is
      rejected if it drops a timecode block / keeps the single-shot lock / omits
      the off-camera voiceover placement.
- [x] `_omni_safety_tail` re-appended "no scene cuts" (would have undone the shot
      list) and its mode tuples did not include `omni_multishot`, silently
      disabling the no-music / persona / reference guards on takes.
- [x] 3 of 24 clips cut off MID-WORD (verified by tail energy: -22 dB of speech
      in the final 120 ms). All three were 5-word sub-shot fragments whose read
      time ranged 1.53s → >4s. Headroom 1.0 → 2.0s.
- [x] Clip creates were uncapped — 24 in the same instant vs a documented ~10
      req/min sibling limit. `OMNI_MAX_CONCURRENCY=6` gates creates only.
- [ ] **Sev-4 render defects** the AI review flagged and nothing in code
      addresses: distorted hands + unreadable product text (00:05-00:11,
      00:56-01:09, 01:10-01:23). Product-label fidelity is the recurring one.
- [ ] **Planner returns an empty timeline on its first attempt** (twice out of
      ~4 attempts; the retry absorbs it). Worth a look — a 50% first-attempt
      failure rate is a real bug, just a self-healing one.
- [ ] Forehead shine / sweat specks appear despite the matte skin clause.

## Next (the actual root cause, planner-side)

`gemini._MAX_BEAT_WORDS = 8` already encodes ~3s beats, but it **yields** for
verbatim copy and for lines with no interior phrase boundary — and this brief is
verbatim, so beats came out 11-20 words. The fix is to make the planner author
~30 beats of ≤6 words, each with its OWN visual_note/framing/broll_shot, by
splitting verbatim copy ACROSS beats at phrase boundaries instead of exempting
it. Then:

- every beat is 2-3s already → no division, so no same-scene jump cuts;
- adjacent non-product beats fold into 9-10s takes of 3-4 REAL scene changes with
  the dialogue running through → ~10 clips, not 29;
- cost drops below today's baseline instead of rising.

- [ ] planner: enforce the word cap for verbatim copy by re-distributing it
      across more beats; require a distinct visual per beat
- [ ] live A/B once beats are 2-3s (cut fidelity still unverified)

---

# TODO — Quality debug round: pacing / variety / adherence / product / anatomy (2026-07-31)

Root causes verified by 5 parallel read-only investigations (file:line evidence
in conversation). Framing vocabulary from user: docs/framing-taxonomy.md.
User bar: shots 2-3s (5s rare), fast decisive motion, strict brief adherence,
no product hallucination, no broken anatomy, visible anchor + screenplay variety.

## WS1 — Shot length & speech pace (video "slow", shots stuck 6s+)
Root causes: validator enforces WORDS (≤16 ≈ 7-10s speech) not seconds;
split_long_windows fires only >8s (comments falsely say 5s); _DUR_MAX=6 is
warn-only; voice_direction "brisk" rule is prompt-only, unvalidated.
- [x] gemini.py: lower _MAX_BEAT_WORDS 16→8 (≈2.5-3.5s speech); allow AT MOST
      one "held" beat/video up to 11 words (the rare earned 5s); verbatim
      scripts exempt from rejection but split at phrase boundaries instead
      (existing _snap/split machinery — deterministic repair, not retry)
- [x] gemini.py plan_timeline prompt: "DEFAULT 2-3s per shot, one ≤5s held
      beat max"; align prose with the new validator numbers
- [x] assembly.py split_long_windows: max_s 8.0→5.0, target_s 7.0→3.0
- [x] pipeline.py: fix stale comments (115-116, 2546-2548) to match reality
- [x] gemini.py: validate voice_direction against slow-pace words
      (slow/unhurried/drawn out/leisurely) — deterministic repair to brisk
- [x] voice.py: log when voice_settings falls back to None (silent except);
      fix stale docstrings (stability=0.5 note, "stability alone" note)
- [ ] config.py: CARTESIA_SPEED/FISH_SPEED get UGC-pace defaults matching
      the EL 1.15 boost (Cartesia speed param; Fish: prosody speed)
- [ ] NOTE cost: 2-3s beats × Seedance 4s floor = more floor waste; log
      per-run estimate delta (Kling b-roll default per July audit offsets)

## WS2 — Motion energy (slow zoom / stuck frame / slow hands)
Root causes: "one very slow, smooth push-in" hardcoded in ALL 4 broll camera
builders; Kling micro-motion table says "slow, deliberate" in all 5 hand
entries; _SETTLE_CLAUSE freezes the last second; talking head prompted
"gently/subtle"; Opus told "static camera" while fallback says "slow push-in".
- [x] pipeline.py 1245/996/1014/1096: replace "very slow push-in" with a
      quick confident push-in / decisive named motion, energy scaled to clip
      length; product shots keep locked camera + FAST subject action (anti-
      morph lock constrains identity, not energy)
- [x] pipeline.py _KLING_MICRO_MOTION: "slow, deliberate" → "quick, precise,
      purposeful" (one completed action, no dawdling)
- [x] pipeline.py _SETTLE_CLAUSE: drop the final-second freeze; "the action
      completes cleanly by the end of the clip" (settle only if clip >4s)
- [x] pipeline.py _talking_motion_prompt: "speaking gently with subtle..." →
      animated, energetic natural delivery (hands talk, head moves)
- [x] gemini.py broll mode-task + video-seedance-skill.md: unify camera rule
      (locked for product, energetic subject motion; pace = brisk UGC)

## WS3 — Screenplay variety + script↔visual match
Root causes: no cross-beat variety rule; hook pre-biased to 3 framings;
temperature=0.7 silently dropped on Claude pro tier (adaptive thinking);
visual_note never validated against dialogue_text; motion prompts never see
the dialogue line; Opus may restage away from the planned visual.
- [x] gemini.py: replace/extend framing enum with the 16-frame taxonomy from
      docs/framing-taxonomy.md (creator movement + camera column per frame);
      two-phase camera frames (High Angle Drop, Action Shot) marked sparing
- [x] beat schema: add camera_direction (the taxonomy camera column) threaded
      beat → write_motion_prompt / f-string builders so the camera actually
      varies per shot
- [x] _validate_timeline: no framing repeated back-to-back; ≥N distinct
      framings per video (deterministic repair: swap dupes from unused pool)
- [x] plan_timeline prompt: explicit VARIETY section; de-bias hook framings
      (any pattern-interrupt frame from the taxonomy)
- [x] script↔visual: planner rule "b-roll visual must depict the CLAIM of its
      dialogue line"; pass the beat's dialogue_text to write_motion_prompt +
      _broll_motion_prompt as context (visual serves this line)
- [x] gemini.py: remove dead temperature=0.7 arg on pro tier (misleading);
      variety comes from prompt rules (opus+thinking ignores temperature)

## WS4 — Brief adherence (verbatim script + visual directives)
Root causes: Mode-A detection heuristic too narrow; zero post-check that
verbatim full_script matches the brief; misclassification routes user scripts
into the word-budget retry loop which SHORTENS them; BRIEF text lands last in
a ~250-line prompt, 210 lines after the "MANDATORY" instruction; brief never
reaches per-beat motion prompts; v1 ScriptDraft has no script_source at all.
- [x] NEW extract step (fast tier): parse brief → {explicit_script?, visual
      directives[], model/character directives[]} BEFORE plan_timeline;
      structured, cheap, testable
- [x] plan_timeline: if extractor found a script → script_source FORCED
      verbatim; full_script seeded from the extracted text; validator
      verifies normalized equality (whitespace-only diffs) and repairs by
      substitution — never retry-shrink a user script
- [x] plan_timeline prompt: move BRIEF + extracted directives to the TOP
      (right after the mandate), rules after
- [x] write_motion_prompt: pass extracted visual directives as a MANDATORY
      block (so brief-level look/screenplay directions survive to i2v)
- [x] word-budget/beat-cap validators: verbatim → split, never reject

## WS5 — Product consistency (standalone product hallucinated)
Root causes: planner writes product visuals BLIND (gets only product_present
bool → hallucinated broll_shot text); i2v prompts FRONT-load that text while
faithfulness clause sits at the tail (Seedance down-weights tails); on
seedance_native the f-string is final (no Opus rescue). Gaps: talking_head
product_in_first_frame passes product as TEXT only (no image ref); broll with
broll_type=None + shows_creator=False → TEXT-ONLY anchor; no re-append guard
for faithfulness after Opus rewrite.
- [x] NEW: one Gemini vision call on the normalized product ref → canonical
      ProductDescription (type/form/colors/label placement); feed to planner
      + prompt writers so product text is grounded, never invented
- [x] product-b-roll motion prompts (all builders): reference-first ordering —
      faithfulness/product-lock clauses front-loaded, shot text becomes
      MOTION+CAMERA only (strip product re-description on uses_product beats,
      deterministic)
- [x] pipeline.py _anchor_refs/_beat_uses_product: attach product ref image
      for ANY beat with product_in_first_frame=True (not just
      product_reveal_pivot)
- [x] _validate_timeline: broll beats must have broll_type (repair: infer
      from visual_note, else lifestyle) — kills the text-only-anchor path
- [x] _write_motion_prompts: add re-append guards for _FAITHFULNESS_CLAUSE +
      _SKIN_TEXTURE_CLAUSE (mirror anatomy/product-lock guards)

## WS6 — Anatomy / physics (laces bent, hands broken)
Root causes: no ban on multi-step dexterity actions (tying/knotting/threading
— i2v models cannot do them); _ANATOMY_CLAUSE guards topology not action
physics; three unwired clauses on native paths.
- [x] gemini.py validator: _DEXTERITY_RE over broll_shot/visual_note (tie,
      knot, lace, thread, braid, weave, fold origami, chop...) → repair to a
      safe adjacent action (planner prompt gets the banned list + preferred
      alternatives: hold/place/tap/lift/turn/point)
- [x] pipeline.py: wire _SKIN_TEXTURE_CLAUSE into _native_vo_broll_prompt +
      _native_silent_broll_prompt; _FAITHFULNESS_CLAUSE into
      _native_talking_prompt (verified unwired W1-W3)

## WS7 — Anchor diversity (female models look identical)
Root causes: character-image-skill.md §1m forces the same 5 "polished pretty
creator" rails on every prompt + single-target look preamble; skin-tone
default bias (fair_golden/medium_wheatish flagged default in BOTH skill MD
§3b and describe_character prompt); _skin_look() hardcodes "naturally dewy
and BEAUTIFUL" for all females; describe_character runs at temp 0.4; 3 of 6
archetypes describe the same woman.
- [x] character-image-skill.md §1m: mandatory-identical rails → "pick 2-3
      polish elements that fit THIS character, vary across characters"; keep
      the polish bar, kill the monoculture (hair-down default removed when
      hair_style says otherwise)
- [x] skill MD §3b + describe_character prompt: remove the two-bucket default
      bias; sample across all buckets unless the brief specifies
- [x] gemini.py _skin_look: derive from character.skin_tone (stay flattering
      per memory — no pore/oil emphasis)
- [x] describe_character: temperature 0.4→0.9 (fast tier forwards temp) +
      prompt nudge to vary hair/setting/wardrobe/region across the space
- [x] CREATOR_ARCHETYPES: differentiate beauty/fashion/default female looks

## Verification
- [x] Tests per WS (validator repairs, prompt content, ref wiring, extractor)
- [x] Full suite green with resolved interpreter (lessons 2026-07-25: verify
      .venv/bin/python target exists first; paste pytest summary)
- [ ] Live acceptance run (user-gated, paid): one brief WITH embedded script
      + visual directives → check verbatim script, 2-3s cuts, varied frames,
      grounded product, fast motion

## Review (2026-07-31)

Shipped, NOT committed. Suite: **628 passed, 3 failed** — the 3 are the
documented pre-existing environmental failures (missing `replicate` module;
ffmpeg/font on caption-burn + bt709 mux), identical to the pre-change baseline
(617 passed + same 3). +11 new tests.

Deviations from the plan-as-written (each deliberate):
- WS1 Cartesia/Fish speeds LEFT at 1.0 — their config comments record a
  measured-first rule ("sonic has not been measured as draggy; raise only
  after listening"; Fish speed is a post-hoc stretch). Bumping them blind
  contradicts the no-speculative-tuning lesson. EL 1.15 boost verified wired.
- WS1 temperature=0.7 on plan_timeline KEPT (not removed): it is dead on the
  Claude pro tier but LIVE on the Gemini fallback provider (test pinned it);
  comment now states both facts.
- WS2 talking-head mouth stays "subtle" (sync-3 re-renders lips; big source
  jaw swings bleed through — QC run v2-20260714-142306); energy raised via
  "lively, energetic presence, never sluggish" instead.
- WS2 _SETTLE_CLAUSE: freeze removed entirely (cut-on-action is correct UGC),
  not gated on clip length; _KLING_FREEZE_RE still strips it for Kling.
- WS3 implemented as _FRAMING_MOTION_CAMERA dict (gemini.py, imported by
  pipeline) + 15 new TalkingFraming values — no new beat schema field needed;
  the framing IS the camera key. Static-rig frames joined
  _STATIC_CAMERA_FRAMINGS (camera_fixed).
- WS3 dialogue-as-context goes to Opus for BROLL modes only — a talking-head
  prompt carrying the line risks the source mouth articulating different
  words than the lipsync pass corrects to (existing test guards this).
- WS5 "product-as-text-only talking heads" gap was NOT real on inspection:
  product_in_first_frame and _anchor_refs share the _beat_uses_product
  predicate, so image-ref and text-mention can't diverge. No change made.
- WS5 verbatim-script mismatch REJECTS with a divergence pointer (retry) —
  partition-preserving substitution isn't computable; whitespace/tag/
  parenthetical diffs are normalized away instead of rejected.
- WS6 dexterity staging REJECTS with stage-before/after guidance (no safe
  auto-repair exists for a semantic restage).
- Brief extractor + product vision description are fail-open (a failure logs
  and the run proceeds as before) and add 2 cheap fast-tier calls per run.
  Neither got a UI toggle — they are correctness steps, not creative knobs;
  flag if you want them exposed anyway.

Live-run watchlist (first paid run answers these):
1. 2-3s beats × Seedance 4s floor = more trim waste per clip — consider
   Kling b-roll (BROLL_VIDEO_PROVIDER, 2.5x cheaper) to offset.
2. The quick push-in on product shots must not reintroduce label morph
   (_PRODUCT_STABILITY_CLAUSE is force-appended; verify on footage).
3. New two-phase frames (high_angle_drop, follow_turn) may misfire on i2v —
   gate them out of the enum if the first run shows breakage.
4. describe_character at temp 0.9: confirm casts actually spread across
   skin/hair/wardrobe now (the two-bucket bias also lived in the skill MD,
   both removed).

---

# TODO — Omni end-to-end video generator mode (2026-07-29)

Plan: `~/.claude/plans/tranquil-purring-heron.md` · Research: `docs/gemini-omni-research.md`
Key decisions: new `video_generator` axis (seedance|omni); NO TTS in omni mode
(synthetic word-budget windows, voice is PROMPTED via voice_persona); reuse
`native_lipsync=True` assembly path; Opus writes omni prompts with own skill
block; run-level seed for voice consistency; clips uploaded to R2 (durable).

- [x] config.py: VIDEO_GENERATORS axis + OMNI_* constants
- [x] video_gen.py: OmniVideoClient (Interactions API, background=True, R2 upload) + `make_video_client(settings, video_generator)`
- [x] Omni prompting skill MD + gemini.py omni modes (own cached system block)
- [x] pipeline.py: video_generator param/validation, omni_mode (both derivation sites), skip VO stages, `_omni_windows`, `_omni_*` fallback builders, clip fork, cost, `_finish_run` native_lipsync, resume round-trip + caption exemption
- [x] assembly.py: trim_keep_audio audio uniformity for concat -c copy — VERIFIED
      NO CHANGE NEEDED: every native-mode section-1 path already re-encodes audio
      to aac/44100/2ch, so 48kHz Omni sources are normalized before the concat
- [x] ui.py: video generator radio + widget gating + threading (partial-run bug
      in the .change wiring found and fixed by the workflow's UI agent)
- [x] tests: test_video_gen / test_pipeline / test_resume omni coverage (test_pipeline.py: unknown-generator ValueError, no-TTS/no-lipsync wiring, run-level seed + 3-10s clamp, `_omni_windows` contiguity/split/merge/hold-span, safety-clause prompt content, captions with/without ElevenLabs key — 165/165 green; test_video_gen.py + test_resume.py already covered by earlier agents)
- [x] full suite green; docs updated

## Review

**Shipped via 11-agent workflow (Opus 5 impl/review/fix, Sonnet 5 mechanical).**
Suite: **3 failed, 617 passed** — the 3 are the pre-existing environmental
failures (missing `replicate` module; ffmpeg/font on caption-burn + bt709 mux),
verified same-on-main earlier.

**Adversarial review found 13 real defects (0 false positives), all fixed.**
The ones that would have silently killed the feature:
1. `persona_clause` was never passed at the Opus call site — every real run
   (llm=claude default) would have generated a DIFFERENT invented voice per
   clip; the voice-consistency bet was dead on the live path while all tests
   passed (they pinned llm="gemini"). Fixed + `_omni_safety_tail` re-appends
   the clause verbatim; Opus now also receives the decapsed/respelled
   `spoken_line` (raw planner text had tags/CAPS).
2. Omni resume hard-required ELEVENLABS_API_KEY (fresh path only warns) —
   paid clips unrecoverable on a key-less box. Now `voice=None` guarded.
3. `_omni_windows` merge could DELETE a beat's dialogue and exceed the 10s API
   ceiling — merge dropped on the omni path; every window clamped to [3,10].
4. Timbre anchor stage ran `ffmpeg -i None` in omni mode — gated to
   seedance_native.

**Voice-consistency risk stands** (run-level seed + verbatim persona clause,
unverified until a paid multi-clip run): treat the first Omni run as the
experiment that answers it.

---

# DONE — multi-provider voice (Cartesia + Fish Audio, 2026-07-29)

Design decisions (user-approved in-conversation; no spec doc by user request):
- Provider selector controls **TTS only**. ElevenLabs stays the authority for the
  char-level master clock (Forced Alignment) and Scribe STT in the QC gate →
  ONE timing path, and it measures the audio that actually plays.
- Matcher is CONSTRAINED to the selected provider's clones (no silent
  cross-provider fallback); empty pool → legacy 8-vibe catalog.
- Clone clip window = longest continuous speech run (ffmpeg silencedetect),
  capped to 10s (Cartesia hard limit) / 25s (Fish 10-30s recommended).
- Best models: Cartesia `sonic-3.5`, Fish `s2.1-pro` (verified against live
  OpenAPI enum + models-overview on 2026-07-29).

- [x] config: cartesia/fish API keys + model/speed/concurrency constants
- [x] voice.py: per-provider text prep (tag strip / tag MAP / de-CAPS)
- [x] voice.py: CartesiaVoice (/tts/bytes mp3), FishAudioVoice (httpx /v1/tts)
- [x] voice.py: AlignedVoice decorator + make_voice_client(settings, provider)
- [x] voice_library.py: provider-aware load + per-provider id resolution
- [x] pipeline.py: thread voice_provider (no new branches)
- [x] ui.py: provider radio + dependent Voice-source dropdown refresh
- [x] scripts/clone_providers.py: clone 26 selected voices × 2 providers
- [x] tests + suite: 545 passed (3 pre-existing env failures: missing
      `replicate` module, ffmpeg/font — verified present on main too)

## Review

**Fish Audio: DONE and live-verified.** All 26/26 selected voices cloned
(`fish_voice_id` in voices.json). Live end-to-end run produced audio + a valid
char-level clock, and `beat_windows` partitioned it into contiguous windows
ending exactly at the VO end.

**Cartesia: BLOCKED on billing, code complete.** `POST /voices/clone` returns
402 `plan_upgrade_required` — "This feature is not available on the free tier"
(upgrade at play.cartesia.ai/subscription). Cartesia *TTS* works fine on this
key (verified: 108KB mp3 from a stock voice via the real SDK, so the
`_write_audio` path and text prep are proven). Only cloning is gated. When the
plan is upgraded, `--provider cartesia` completes the feature with no code
change. Until then selecting Cartesia raises a clear actionable error (0 clones)
rather than sending it ElevenLabs ids.

**FISH_MODEL defaults to `s2.1-pro-free`, deliberately.** Same weights and
language coverage as `s2.1-pro`, $0, no TTFA/DPA SLA. Measured: this account
402s on `s2.1-pro` ("Insufficient API credit" — Fish API credit is separate
from platform credit) while the free tier returns audio. Batch pipeline never
consumes a latency SLA. Flip via FISH_MODEL once credit is topped up.

**Bug caught during live verification (the important one).** The first working
run force-aligned against the PROVIDER text, so the clock read
`"[excited] It is [emphasis] insane"`. `assembly.beat_windows` locates a beat
via `align_text.find(_strip_tags(dialogue_text))`, and `assembly._strip_tags`
removes brackets but PRESERVES CAPS — so `"It is INSANE"` could never be found,
`find()` would miss, and the cursor fallback would silently drift every later
cut. Fixed by splitting the two texts: the provider gets provider-specific
markup (delivery), the aligner gets `spoken_text(script)` — tag removal only,
byte-faithful, no whitespace collapsing, no case change. `spoken_text` is pinned
to `assembly._strip_tags` by test so they cannot diverge.

# TODO — flat voice + bad pronunciation (2026-07-29)

Root cause (systematic-debugging, Phase 1): TWO separate causes.
- FLATNESS (all modes): `gemini.py` capped audio tags at 1-2 in the WHOLE script,
  while tags ARE eleven_v3's modulation mechanism; compounded by stability=0.5
  (the less responsive of the two acceptable modes).
- FLATNESS (native): the video model IS the TTS, and the prompt carried NO vocal
  emotion direction at all — only identity, rate, and a per-section tone that
  describes the FACE.
- PRONUNCIATION: Seedance's language set excludes Hindi and has no lexicon param;
  ALL-CAPS reached it unmodified (decaps_emphasis was wired only to Cartesia).

- [x] gemini.py: tag cap -> ~1 delivery tag/sentence + curated allowlist;
      audible REACTION tags still capped at 2 (each opens an alignment hole)
- [x] config: ELEVENLABS_STABILITY=0.0 (Creative — the mode that acts on tags)
- [x] voice.py: dropped similarity_boost (verified dead on v3)
- [x] gemini.py: `pronunciations` + per-beat `voice_direction` emitted by the
      DIRECTOR (no static tables — user correction, see lessons.md)
- [x] voice.py: EL alias dictionary (clock-safe, verified) + local substitution
      for Cartesia/Fish + `spoken_text` split keeps captions canonical
- [x] pipeline.py: native prompt gets the director's delivery direction, and the
      spoken line is respelled + de-CAPSed
- [x] assembly.py: corrected the false "API consumes the tags" docstring
- [x] 558 tests pass (3 pre-existing env failures)

## Review / open

Verified live (no EvoLink spend): director emits Kesharaj->KAY-shuh-raj,
bhringraj->bring-RAAJ, amla->AAM-laa and per-beat direction tracking the arc;
alias dict changes audio (0.507s->1.040s) while alignment keeps real spelling;
beat windows stay contiguous at 3 tags (max gap 0.000000s). A/B audio in
scratchpad/vo_BEFORE.mp3 vs vo_AFTER.mp3.

HARD CEILING on native (research): ByteDance tuned expressiveness flat on purpose
("avoiding over-exaggeration" vs Sora 2), our image+audio reference mode scores
2.37/5 on Reference Alignment in their OWN launch paper (worst of all modes), and
there is NO prosody/pitch/rate/pronunciation parameter in any Seedance API.
=> native will improve PARTWAY only. For expressive, correctly-pronounced
Hinglish use syncso/heygen_replicate over ElevenLabs (or Fish) audio.

- [x] Tail fade on native speaking clips (assembly._NATIVE_TAIL_FADE_S=0.08,
      env-tunable) — vendor-acknowledged end-of-clip click. Verified on real
      audio: final 50ms -21.0dB -> -32.9dB; skipped when clip < fade.
- [ ] DELIBERATELY NOT VERIFIED (user declined the paid probes, 2026-07-29):
      does phonetic respelling actually fix pronunciation in native? does
      de-CAPS matter? does an expressive anchor transfer expressiveness?
      Zero public evidence exists either way for all three — they follow vendor
      PRINCIPLE but are unproven. Probe cost would be ~$4-6 (Seedance ~$0.99 per
      5s clip). The next real native run is the test.
- [ ] Tag density landed at 3 across 6 beats; prompt still has headroom toward
      the ~1/sentence target.

---

# TODO — finishing round 2 (UNCOMMITTED — user gate, 2026-07-09 evening)

- [x] Native-mode captions via ElevenLabs forced alignment (voice.force_align → assembly native branch; failure = skip, never fail)
- [x] Audio correction chain: highpass 100 → deesser → mud-cut/presence/air EQ → 2.5:1 comp → 2-pass loudnorm; LOUDNORM_I env-tunable (−14 default); NO dual_mono (delivered stereo measures true)
- [x] Glitch feedback: rgbashift REMOVED, grain 8→5, punch 10%/4f → 6%/5f
- [x] app/grading.py: looks clean_bright (default) / warm_golden / teal_orange / none — research recipes visually recalibrated on real footage (raw research values blew skin out); huesaturation colors=r+y flags syntax
- [x] Shot matching: median-referenced capped corrections baked into per-clip re-encodes (incl. lipsynced heads via strip_audio extra_vf); COLOR_MATCH_CLIPS=True
- [x] app/qc.py: Gemini defect-audit (2fps video + 3 keyframes, evidence-gated schema, report-only) → manifest + UI accordion; live-tested on v2-...-203837: correctly flagged warped hands (sev5), native lipsync flapping (sev4), plastic skin, missing captions
- [x] UI: Colour look radio, shot-matching + AI-review checkboxes
- [x] 382 tests green. NOT committed — user asked to gate commits.

---

# TODO — production finishing (feat/production-finishing, 2026-07-09)

Plan: ~/.claude/plans/wobbly-gliding-stonebraker.md · Research: docs/production-polish-plan-2026-07.md

- [x] Phase 1 — Captions v2: font vendored, resolution-aware style, tag strip, chunk-pop build_ass, emphasis, tests
- [x] Phase 2 — Wiring: per-beat alignments persisted, per-mode caption timing in assemble, config/UI toggles (ALL finishing controls in UI per user)
- [x] Phase 3 — Finishing pass: master_audio (room tone + fades + 2-pass loudnorm), finish() single encode (grade→grain→vignette→subs, BT.709 via setparams, 192k), grade_phone_capture/overlay_audio removed
- [x] Phase 4 — Hook/CTA: product_name/hook_text/cta_text in planner schema + ASS Hook/CTA events
- [x] Phase 5 — Transitions: TRANSITION_STYLE punch/slide/cut on b-roll heads + product punch-in (user asked "why no transitions" → crossfades still excluded: corporate tell + desync; accents instead)
- [ ] Phase 6 (deferred) — cross-clip color matching: untested color math, needs its own visual A/B PR
- [x] Verification: 370 pytest green; lavfi e2e → −14.0 LUFS, bt709×3, aac/48k, exact isolated caption offsets; frames visually checked (caught WrapStyle overflow bug → fixed)
- [ ] REMAINING: real 1080p run (API credits) to check caption sync on real TTS, grade on skin, pop feel; A/B FINISHING_GRADE off

---

# TODO — brief-adherence + native hardening + 1080p + sync.so quality (2026-06-14)

Triggered by run v2-20260614-001634 (Ustraa Base Camp, native mode): the brief's
IMPORTANT shot ("unseen presence of someone, soaking in the lingering perfume,
esp. during 'Girls love fresh, masculine fragrances…'") did NOT appear at all,
and the chosen "sensuous" remix voice was lost.

## Root causes (verified against the run)
- Director encoded the important shot as a 0.9s SILENT reaction hold
  (beat 4: beat_kind=reaction, shot_type=talking_head).
- Native `merge_short_windows` skips B-roll but NOT reaction beats → the 0.9s
  beat was merged away (11 beats → 9) BEFORE apply_holds ran. Shot gone.
- Native r2v RE-RENDERS the VO → the sensuous ElevenLabs remix was thrown away.
- Real 1080p is blocked by assembly's fixed `_W,_H = 720,1280` re-encode.

## Plan (user-approved: harden native + sync.so default; 720p default, 1080p opt-in)
- [x] A. Director: "BRIEF DIRECTIVES (MANDATORY)" block; brief-flagged important
      shot → real multi-second B-ROLL cutaway under VO (never a sub-second
      reaction); tightened reaction-beat guidance. (gemini.py)
- [x] B. Harden native: `merge_short_windows` skips reaction beats (`_is_reaction`).
- [x] C. 1080p opt-in: clip_resolution control (720p default) → Seedance `quality`
      AND assembly encode dims (`_RES_DIMS`); manifest records it. (config/ui/pipeline/assembly)
- [x] D. sync.so quality: encode at the run's dims (no downscale of 1080p sync output);
      `_CRF` 18 → 16. (assembly.py)
- [x] G. (added mid-task) Product-shot hands = creator's own (not a man's hand):
      product_closeup/product_in_scene with shows_creator → hands-only citation +
      creator-hand wording. (pipeline `_anchor_refs`, gemini `write_broll_prompt`)
- [x] H. (added mid-task) Cap/spray logic: never spray a sealed bottle; dropped
      "spritz" from `_PRODUCT_HOLD_CLAUSE`, cap-stays-on rule in Director + broll prompt.
- [x] E. Tests per item + full suite green → 257 passed.
- [ ] F. Verify end-to-end on sync.so with the Ustraa brief (USER acceptance run).

## Review (2026-06-14)
All code + tests landed (257 passing, +5 new). Files touched: app/gemini.py (brief
mandatory block, reaction guidance, creator-hand + cap rules in product blocks &
write_broll_prompt), app/pipeline.py (_anchor_refs hands routing for product shots,
clip_resolution threading, _PRODUCT_HOLD_CLAUSE cap rule), app/assembly.py
(merge_short_windows skips reactions, _RES_DIMS + _CRF + resolution-aware encodes,
assemble clip_resolution), app/config.py (DEFAULT_CLIP_RESOLUTION), app/ui.py
(clip_resolution control wired end-to-end). Memory + lessons updated.
Next: a sync.so run of the Ustraa brief to confirm — (1) the lingering-scent shot
appears as a real visible beat; (2) sensuous remix voice intact; (3) product shots
show HER hand, cap on, no spray; (4) 1080p toggle yields a 1080x1920 final.

---

# Native Seedance audio-driven lipsync (+ fundamentals fixes)

Context: docs/naturalness-audit-2026-06.md. User decision: add a UI-selectable mode where
each talking-head beat's VO slice is passed to Seedance as a reference-audio URL (with the
beat's dialogue quoted in the prompt), so Seedance generates the clip already lipsynced —
no sync.so. The existing master-clock model (continuous VO → beat_windows → trim →
concat → overlay) is kept unchanged; only the lipsync mechanism is swapped.

## Phase 0 — Gate experiment (~$3, BEFORE any code lands)
- [x] Probe EvoLink: does `/videos/generations` accept an audio-ref field (`audio_urls`
      mirroring `image_urls`? check EvoLink docs/new-api passthrough) for
      seedance-2.0-image-to-video?
- [x] One real beat end-to-end: real-human anchor + 3-4s ElevenLabs wav slice + quoted
      dialogue prompt. Verify: (a) not blocked by moderation, (b) speech starts at t=0
      and matches the slice, (c) mouth+body visibly paced to the audio, (d) cost per
      second vs current.
- [x] If EvoLink rejects audio refs → fallback probe: Runware (hedge per provider audit)
      or fal Kling AI Avatar v2 for the native path. Report findings, then proceed.

## Phase 1 — Fundamentals (both modes benefit; safe regardless of gate outcome)
- [x] `slice_audio`: move `-ss` after `-i` (sample-accurate slices; feeds BOTH sync.so
      and the new native path) — assembly.py:222-229
- [x] `overlay_audio`: replace `-shortest` with `-t {alignment.duration}`; make the
      drift check fail >0.15s instead of warn at 0.5s — assembly.py:232-239, 359-368
- [x] `strip_audio`: add `-t window_len` + assert vs `LipsyncResult.output_duration`
      (sync.so path) — assembly.py:210-219, lipsync.py:87
- [x] sync.so path: lipsync FULL ceil-second clip vs full-span VO slice, trim AFTER
      (ai-ugc-research.md:178 fix) — assembly.py:278-334
- [x] Thread `camera_fixed` through VideoRequest/_build_body; True for static framings
      — video_gen.py:44-55, 295-310
- [x] Window-aware gesture line in `_talking_motion_prompt` ("complete gestures within
      the first {window_len:.1f}s") — pipeline.py:231-251 (matters mainly for sync.so
      mode; harmless in native mode)
- [x] Sub-beat anchor continuity: mid-action anchor description for split sub-beats
      — assembly.py:155-181 / pipeline.py:444-466

## Phase 2 — Native lipsync mode
- [x] `VideoRequest`: add `audio_url: str | None = None`; `_build_body` emits the
      audio-ref field per gate-experiment findings (incl. correct `generate_audio`
      semantics when an audio ref is present — gate item) — video_gen.py
- [x] Config/UI: `lipsync_engine: "syncso" | "seedance_native"` toggle in the v2 tab
      (default stays syncso until native is validated) — config.py, ui.py
- [x] NEW native talking-head prompt template: dialogue IN QUOTES
      (video-seedance-skill.md rule) + delivery/emotion matching the audio tone;
      DROP the "mouth clearly opening and closing" sync-3 hack (it fights the
      reference audio); keep iPhone/UGC trigger + visual_note — pipeline.py
- [x] Pipeline `_clip()` native branch: upload per-beat VO slice (slice_audio →
      uploader), pass as audio_url — pipeline.py
- [x] Planner: MIN beat length for talking_head (≥~5 words ≈ 2.5s est; Seedance
      audio-ref min is 2s) — gemini.py validator
- [x] Post-VO safety merge: any measured talking-head window <2.2s merges into its
      neighbour before generation — assembly.py/pipeline.py
- [x] Planner: audio tags may only OPEN a beat's dialogue, never close it (trailing-tag
      audio otherwise leaks into the next beat's slice; verified finding B3) — gemini.py
- [x] `app/pacing.py` silence-trim BEFORE beat_windows (required in native mode: dead
      air in a slice gets animated as the creator standing mute), rescaling alignment
      timestamps — new module + pipeline.py:411
- [x] `assemble()` native branch: skip sync.so stage for talking_head; QC: ffprobe the
      returned clip's baked audio ≈ slice duration (catch shifted/regenerated speech);
      strip baked audio (keep continuous-VO overlay = gapless master clock); assert
      clip duration ≥ window — assembly.py
- [x] split_long_windows: each sub-beat gets its own slice (each ≥2.2s — adjust
      target_s if needed); keep 5s max default, expose as config for natural-pace runs
- [x] Manifest: record audio_url + engine per clip for resume/debug

## Phase 3 — Validate
- [x] Unit tests: body building with audio_url, native-mode assembly skip, planner
      ≥2.2s validator, slice accuracy (output-seek) — 196 passing
- [ ] One full real run in native mode; side-by-side vs sync.so run on the same brief;
      check: lipsync, body-speech coupling, cut energy, A/V drift == 0
- [ ] Cost comparison logged; update memory + docs with verdict

## Claude LLM provider (2026-06-10) — DONE
- [x] `app/claude.py` ClaudeBackend (AsyncAnthropic, lazy import, `messages.parse` for
      structured outputs, prompt-cached system blocks, refusal → ValueError)
- [x] `GeminiClient._generate` seam routes all 10 task methods by `llm_provider`
      ("claude" default, "gemini" behind UI toggle); image_gen stays Gemini (NB)
- [x] Per-task models: plan_timeline + write_seedance_prompt → claude-opus-4-8
      (adaptive thinking, effort high, no temperature); 7 fast tasks → claude-sonnet-4-6
      (effort medium, temp passthrough); should_open_with_product → claude-haiku-4-5
- [x] config: ANTHROPIC_API_KEY + LLM_PROVIDER (default "claude"); pipeline fail-fast
      validation before any paid call; UI "Script LLM" radio; manifest records provider
- [x] 216 tests green (16 new in test_claude.py); `anthropic>=0.109.1` added via uv
- [ ] USER ACTION: add ANTHROPIC_API_KEY to .env before first Claude-mode run

## Review (2026-06-10)
All Phase 0-2 items landed and adversarially verified (2 workflows, 25 agents total;
196/196 tests green). Key deviations from the original plan, forced by the Phase-0 probe
(tasks/gate-phase0-results.md):
- Native mode uses `seedance-2.0-reference-to-video` (NATIVE_LIPSYNC_MODEL) — the i2v
  model HARD-REJECTS audio_urls (HTTP 400). Lipsync quality on the probe clip: 9/10,
  exact words, same voice, no artifacts. Same per-second price as i2v std.
- r2v audio is RE-RENDERED (±0.3s internal offset), so native assembly KEEPS each
  talking-head clip's baked audio (speech-end-aware trim: min(src_dur, max(window,
  speech_end+0.12))), b-roll gets its VO slice attached, concat carries per-beat audio,
  and there is NO continuous-VO overlay in native mode (drift structurally impossible).
- `camera_fixed` emitted only when True; native branch sends False (r2v acceptance of
  the key unverified — a 400 would kill a fully-billed clip stage).
- `_native_talking_prompt` ends with the probe's working binding sentence: "Her voice
  and lip movements follow Audio 1."
Remaining: Phase 3 real-run side-by-side (native vs syncso on the same brief) + cost log.
Polish candidates after first real run: loudness seam between baked r2v audio and VO
slices at talking-head↔b-roll cuts (consider loudnorm); seedance-2.0-fast-reference-to-video
(untested, cheaper).

## Director rebuild (2026-06-12) — SHIPPED (workflow wf_2b0b3c8f-3ef, 251 tests green, 6/6 verdicts fixed)
Approved plan: ~/.claude/plans/so-plan-eveything-and-linear-koala.md. All in subagents.
- [x] A — Director scene-first planner: plan_timeline rewritten (one opus call, scene-driven
      shots not word-count); describe_character runs FIRST (brief-only); new beat fields
      beat_kind/hold_s/shows_creator; _validate_timeline face + illogical-hands guards.
- [x] B — Identity on every person-shot: _anchor_refs driven by shows_creator (fixes the
      lifestyle-broll "different woman"); face-unobstructed clauses + eyewear pushed-up.
- [x] C — Authored holds: assembly.apply_holds inserts real silence for reaction beats
      (mirrors trim_silences, shifts alignment); reaction beats never lipsynced. TTS script
      stays clean — silence is structured, not [silence] text.
- [x] D — Native silence-padding: pad_audio_to pads each r2v reference to clip length
      (probe-validated fix for stretch/repeat).
- [x] E — Anatomy/camera: product/hands beats camera_fixed=True; one-pair-of-hands /
      no-phantom-limbs / no-morph bans; one motion verb + one camera move.
- [ ] ACCEPTANCE (user): run Ustraa brief on sync.so (same woman everywhere, sane staging,
      scene-driven shot count, one pair of hands on product); then once on native (no
      repeated/cut dialogue — manifest["native_audio"] speech_end ≈ line length).
- [ ] Optional if anatomy still slips: Gemini vision-QC re-roll on rendered clips (flagged
      stretch in plan).

---

## Review — Seedance realism fix (plastic/AI-look) — 2026-06-14

Research: 20-agent workflow (5 web-research → 14 adversarial fact-checks → synth).
Verified guide: `docs/seedance-realism-guide.md`. Root cause: the skin-texture/optics
tail was specced in the old guide but NEVER wired into the live prompts; resolution
alone (user tried 1080p) can't fix it.

Shipped (all 259 tests pass):
- [x] `app/pipeline.py`: added `_SKIN_TEXTURE_CLAUSE`, `_PRODUCT_TEXTURE_CLAUSE`,
      `_FAITHFULNESS_CLAUSE`; wired into `_talking_motion_prompt` (texture early +
      faithfulness + even/diffuse lighting), `_native_talking_prompt` (texture early),
      `_broll_motion_prompt` (lifestyle=grain tail, product=material realism + drop the
      irrelevant product-stability ban on lifestyle).
- [x] `app/ui.py`: positive micro-texture cues in both anchor prompts; fixed the
      self-contradiction (`no fake imperfections/textures` → `no plastic-CGI texture`).
- [x] `app/assembly.py`: `grade_phone_capture()` ffmpeg "real phone capture" pass +
      wired into `assemble(phone_capture_grade=...)`, audio-preserving, applied once.
- [x] `app/config.py`: `PHONE_CAPTURE_GRADE=False` (opt-in; grain can dirty labels →
      needs visual A/B), `HERO_CLIP_RESOLUTION="1080p"` (finals-only).
- [x] Tests: raised prompt word-count bounds w/ rationale; new
      `test_prompts_carry_skin_texture_defense` + `test_grade_phone_capture_preserves_audio_and_duration`.
- [x] Item 7 (anchor image_size): verified-sufficient at "2K" ≥ ≤1080p clip res; no change.

Next (user): A/B with pinned seed trio on fast/720p — Run 0 baseline vs Run 1 (prompt
tail). Only enable PHONE_CAPTURE_GRADE / 1080p after Run 1 confirms the prompt win.

---

## Opus-written Seedance motion prompts (v2) — 2026-06-16

Spec: `docs/superpowers/specs/2026-06-16-opus-seedance-prompts-design.md`
Plan: `docs/superpowers/plans/2026-06-16-opus-seedance-prompts.md`
Goal: v2 Seedance motion prompts written per-beat by Claude Opus (revised skill MD as
system, anchor frame as multimodal image, timeline-aware, concurrent), f-string
fallback, debug-rich manifest. Re-executed fresh via an Opus 4.8 workflow
(wf_f4acec6a-7a5): implement → tests-after → multi-lens verify. NO commits.

- [x] Task A: revise `video-seedance-skill.md` — de-duplicated, realism digest folded in
      (flattering skin texture, preserve-composition-and-colors, even/diffuse lighting,
      only-safe `no 3D/cartoon/VFX` negation, product-no-grain), length retuned to tight
      front-loaded ~60–100w i2v, worked example replaced; v1 path preserved.
- [x] Task 1: `image_url` on `ClaudeBackend.generate` (multimodal url source).
- [x] Task 2: thread `image_url` through `GeminiClient._generate`.
- [x] Task 3: `MotionPrompt` schema + `write_motion_prompt` (Opus, skill+timeline, image).
- [x] Task 4: pipeline helpers `_beat_mode` / `_fallback_prompt` / `_timeline_overview`.
- [x] Task 5: `_write_motion_prompts` stage (concurrent, Opus+fallback, debug manifest).
- [x] Task 6: wire stage 5b into `generate_ugc_video`; `_clip` consumes `motion_records`.

Post-verify fixes (controller-applied): (1) empty-string `image_url` → truthiness guard
so `""` degrades to text-only; (2) `_clip` now derives `is_reaction`/`native` from
`_beat_mode` (single source of truth — kills the duplicated mode-classification risk).
Verify: full suite **289 passed**, 0 failed; +30 new tests. Nothing committed.

Camera-fixed lock (resolved 2026-06-16): user trusts Opus to honor the camera direction
— NOT adding a hard lock. Opus already has the rule (broll mode-task: "static, locked-off
camera — a moving camera over a product is the #1 morph source") + the locked-tripod
f-string as its baseline-to-beat. Left to Opus's judgment by design.

---

## Sync.so experiment: whole-video lipsync + sync-3 param knobs (2026-06-24)

User wants to A/B the current per-clip lipsync against sending the WHOLE stitched
video + the full VO to sync.so in one pass, AND to stop leaving sync-3 at defaults.
Docs research (subagent + pasted OpenAPI): syncsdk 0.3.0 GenerationOptions DOES accept
temperature + occlusion_detection_enabled; whole-video is technically supported but
costs MORE (per-second billing now also covers b-roll/reaction seconds) and has no
"skip this segment" flag — experimental by nature. Default stays per_clip.

## Design decisions
- New config knobs (all env-overridable):
  - `SYNC_OCCLUSION_DETECTION` (default **True** — user asked to turn it on)
  - `SYNC_TEMPERATURE` (default 0.5)
  - `SYNC_LIPSYNC_GRANULARITY` = `per_clip` (default) | `whole`
- Whole-video mode is **syncso-only**; heygen (needs per-clip muxing) / seedance_native
  (audio-driven) raise a clear ValueError.
- Whole mode: talking-head clips trimmed silently like b-roll → concat → ONE sync.so
  call bakes the VO in → no per-clip overlay. `n_lipsynced = 1`. Resume-safe via a
  `whole_video: true` record in manifest["lipsync"].

## Tasks
- [x] config.py — SYNC_OCCLUSION_DETECTION, SYNC_TEMPERATURE, SYNC_LIPSYNC_GRANULARITY (+ SYNC_GRANULARITIES)
- [x] lipsync.py — SyncSoLipsync passes temperature + occlusion_detection_enabled
- [x] assembly.py — `whole_video_lipsync` param + single-pass branch + resume
- [x] pipeline.py — `lipsync_granularity` param, validation, manifest, plumb to assemble
- [x] ui.py — granularity radio wired through on_generate_v2
- [x] tests — lipsync options; assemble whole-video single pass + resume; pipeline validation
- [x] verify — full test suite green (301 passed; +6 new), all 5 modules byte-compile

## Review (2026-06-24)
Shipped, all 301 tests green (+6: 1 lipsync, 2 assembly, 3 pipeline). No commit made.
- **Already on the SDK:** SyncSoLipsync uses syncsdk's AsyncSync.generations.create — the
  new options go through GenerationOptions (verified syncsdk 0.3.0 exposes temperature +
  occlusion_detection_enabled). Only HeyGen uses raw httpx (no Python SDK exists).
- **occlusion ON by default** (SYNC_OCCLUSION_DETECTION), **temperature 0.5** (SYNC_TEMPERATURE),
  both env-overridable; HeyGen ignores them (sync-3 only).
- **whole-video mode:** trims every beat silently → concat → ONE Sync.so pass on the full
  cut + full VO → output carries the VO (audio_baked_in routes it through the native
  copy/burn finish, no double overlay). n_lipsynced=1. Resume-safe via whole_video record.
  syncso-only; heygen/native raise. Default stays per_clip.
- **Knobs are opt-in env vars** (sensible defaults), so no .env edit needed. There is no
  .env.example in the repo (lipsync.py's error strings reference one — pre-existing).

### Caveats to flag to the user before the experiment run
- whole-video costs MORE than per-clip (you now pay sync-3's per-second rate for b-roll +
  reaction seconds that per-clip never sends).
- sync-3 has no "skip" flag → silent reaction beats get forced to "speak" the VO, and any
  face in b-roll gets driven. Cleanest A/B is on a talking-head-heavy ad.

---

# Seedance Native Timbre Voice (2026-06-29)

Design: docs/seedance-native-timbre-voice-design.md
Plan:   docs/seedance-native-timbre-voice-plan.md
Branch: feat/seedance-native-timbre-voice  · Ledger: .superpowers/sdd/progress.md

- [x] T1 Scribe transcribe + script_fidelity (QA-gate primitives)
- [x] T2 VoicePersona + describe_character
- [x] T3 voice_mode field (on_camera/background_vo/silent) + resolver
- [x] T4 single reused timbre anchor + multilingual anchor voice
- [x] T5 native-timbre prompt builders (timbre-ref, strict fidelity)
- [x] T6 dispatch routing + share one anchor (drop per-beat slicing)
- [x] T7 anchor lifecycle in generate_ugc_video
- [~] T8 per-clip fidelity gate + syncso fallback — DROPPED (deferred; revisit if drift appears)
- [x] T9 native-timbre assembly routing + UI/config labels
- [ ] T10 live validation suite (CREDIT-GATED: EvoLink 51.34/108)

---

# Sync.so Isolated Per-Beat Audio (2026-07-04)

Design: docs/syncso-isolated-audio-design.md
Plan:   docs/syncso-isolated-audio-plan.md
Branch: feat/seedance-native-timbre-voice

syncso per_clip switches from continuous-VO slicing to the heygen_replicate
isolated-audio topology (per-beat TTS ↔ clip by index, audio-derived clip
duration, pad-to-exact, audio baked in, no VO overlay).

- [x] T1 lipsync.py: drop dead sync-3 options (temperature/occlusion); ReplicateHeyGen video_needs_audio=True
- [x] T2 assembly.py: rename replicate_lipsync→isolated_audio; mux gated on video_needs_audio; output_url record key
- [x] T3 pipeline.py: isolated_audio predicate threaded through _beat_mode/_fallback_prompt/TTS/_clip/assemble
- [x] T4 config granularity docs + full-suite verification + review section

## Review (2026-07-04)
- syncso per_clip now runs the isolated-audio topology: per-beat TTS ↔ clip by
  index, clip ordered at max(4, min(15, ceil(audio))), audio padded to exactly
  the clip length (sync_mode="silence" belt-and-braces), synced audio baked in,
  no continuous-VO overlay. Mapping/mismatch handling is byte-identical to the
  proven heygen_replicate path — same code, provider swapped.
- Provider split lives in ONE place: assembly muxes audio into the input video
  only when the client sets video_needs_audio (HeyGen dub models); Sync.so gets
  the raw silent clip + separate audio (one less re-encode per beat).
- Fixed latent contradiction: ReplicateHeyGenLipsync claimed
  video_needs_audio=False while assembly unconditionally muxed — flag now True.
- Dropped dead sync-3 config (temperature, occlusion_detection_enabled —
  removed server-side per sync.so docs); GenerationOptions sends sync_mode only.
- Legacy continuous-VO slicing survives ONLY for direct heygen engine and
  syncso "whole". seedance_native untouched.
- Verification: 358 passed (full suite), incl. new tests: syncso-isolated
  no-mux upload, _beat_mode/_fallback_prompt isolated routing, kwarg plumbing.

## Review — plan_timeline mid-sentence crash fix (2026-07-14)
- [x] Root-caused live crash: retry feedback carried only the LAST error → corrections ping-ponged (mid-sentence → empty beats → identical mid-sentence) until exhausted
- [x] `_with_retry` now accumulates ALL distinct rejections into the correction block
- [x] Fundamental fix (user-requested): `_snap_boundaries_to_phrase_edges` — mid-sentence boundaries snap to the nearest legal cut position in code; beats straddling an unsplittable sentence merge (longer member's shot design, cta-last preserved, indexes renumbered); anatomy checks moved post-repair
- [x] Full suite green: 407 passed; live-failure shape reproduced and verified repaired

---

# Plumb cloned voices into the pipeline — character-first voice matching (2026-07-22)

Goal: planner picks the best CLONED voice to MATCH the generated character
(fixes the client "voice didn't match the character" complaint). Character
gender/age/etc still come from the BRIEF; the voice is chosen to fit the
character. Cloned library is the default pool; catalog is fallback when no
clone genuinely fits (e.g. older character vs all-early-20s clones).

- [ ] app/voice_library.py: VoiceProfile + load_voice_library() from data/voice_clones/voices.json (selected + has voice_id)
- [ ] GeminiClient.match_voice(character, profiles): gender-filter, LLM best acoustic match or none → catalog fallback
- [ ] app/config.py: USE_CLONED_VOICE_LIBRARY, VOICE_LIBRARY_PATH
- [ ] app/pipeline.py: resolve voice as override > library-match > catalog vibe; log source+reason
- [ ] app/ui.py: voice-source control (Auto-match / specific clone / catalog) + keep raw override
- [ ] tests: loader, precedence, no-fit fallback
- [ ] verify end-to-end (dry)

## Review (2026-07-22) — DONE
- app/voice_library.py: VoiceProfile + load_voice_library() (26 selected clones) + candidates_for_gender (gender hard-filter, ambiguous eligible for both).
- GeminiClient.match_voice(character, profiles): gender-filtered candidates, LLM picks best acoustic match to the ALREADY-CAST character (age-closeness first, then energy/timbre/vibe); returns voice_id=None when no genuine fit; hallucinated-id guard → None.
- config: USE_CLONED_VOICE_LIBRARY (env, default ON) + VOICE_LIBRARY_PATH.
- pipeline: voice precedence = user override > cloned-library match > catalog vibe (fallback). Character generation UNCHANGED (gender/age still from brief). manifest.voice now carries source + match_reason.
- ui: "Voice source" dropdown (Auto-match / specific clone / legacy catalog) + raw override still wins; per-run use_voice_library threaded through generate_ugc_video; voice panel shows source + match reason.
- tests: tests/test_voice_library.py (6) — load/filter, gender candidates, match returns/nulls/short-circuits. Updated _RecGemini stub. Full suite 421 passed.
- Verified with REAL Gemini + live library: young-female→early-20s youthful clone; older-male(42)→closest mid-30s clone (pool skews 20s; catalog remains fallback for true no-fit).

---
# Product size/label consistency fix (2026-07-22) — DONE
- Root cause: Seedance i2v motion; `_PRODUCT_STABILITY_CLAUSE` lacked size-lock + label-adhesion; Opus (active path) could paraphrase product-lock away (only anatomy was force-guaranteed).
- Fix: (1) strengthened `_PRODUCT_STABILITY_CLAUSE` — intrinsic size/scale lock (push-in allowed) + label printed-flat/adhered, transparent-aware; (2) force-append the clause on `uses_product` motion beats in `_write_motion_prompts` (mirrors the anatomy guarantee); (3) reinforced the still-image product clause for transparent packaging.
- Tests: 3 new (clause content, force-append on product beat, no-append on non-product) + updated 1 existing wording assertion. Full suite 424 passed.

---

# Kling v3 b-roll provider + resume a partially-paid run (2026-07-25)

Design: docs/superpowers/specs/2026-07-25-kling-broll-provider-and-resume-design.md

Why: 72,051 cr = $1,060.42 spent over 119 runs (median $8.91/run), ~$100 left.
Two leaks: (a) b-roll seconds billed at Seedance's 13.5 cr/s when Kling v3 does the
same job at 5.4 cr/s on the SAME EvoLink endpoint/key (~2.5x cheaper, ~36% off a
typical run); (b) $25.64 of paid clips stranded across 5 abandoned runs because
resume is dead code. Kling is DEFAULT OFF — today's output is the quality bar.

## Phase 1 — config + client (Kling body dispatch)
- [ ] config.py: BROLL_VIDEO_PROVIDER="seedance", BROLL_VIDEO_PROVIDERS, KLING_BROLL_MODEL, KLING_QUALITIES (DEFAULT_X + XS-tuple convention)
- [ ] video_gen.py: rename EvoLinkSeedanceGenerator → EvoLinkVideoClient + keep the old name as a compat alias (chain.py/tests); update make_video_client annotation
- [ ] video_gen.py: VideoRequest.negative_prompt (Kling has the field, Seedance does not)
- [ ] video_gen.py: provider_for_model() — kling*/seedance*, else ValueError (fail fast on unknown family)
- [ ] video_gen.py: _build_body → dispatch; _seedance_body = today's logic byte-identical; _kling_body (image_start, sound="off" STRING, int duration, no seed/camera_fixed/aspect_ratio/generate_audio/image_urls; raise on audio_url); _kling_quality (480p clamps UP to 720p)
- [ ] LEAVE poll/retry/deadline + create_task_async/poll_task_stream_async untouched (the 914536f hardening — do not fork)

## Phase 2 — routing predicate + Kling prompts
- [ ] pipeline.py: _use_kling(mode, *, native, broll_provider) — broll/reaction/silent/background_vo only; the `native` guard keeps audio-driven background_vo on Seedance r2v
- [ ] pipeline.py: _KLING_NEG_PRODUCT / _KLING_NEG_PERSON (bare noun phrases, never "no X"; skin negatives stay flattering — plastic/waxy/blotchy banned, no pore/oil emphasis)
- [ ] pipeline.py: _kling_broll_prompt() → (positive, negative); motion-only ≤400 chars, no product re-description (Kling's documented cut/transition trigger), camera lock = omit camera words + negative
- [ ] pipeline.py: reaction path drops _SETTLE_CLAUSE for a NAMED micro-motion (Kling returns frozen clips on vague prompts)
- [ ] pipeline.py: deterministic validator/repair — sentence-boundary truncate, strip camera words when locked, guarantee the preserve-composition tail exactly once (Opus paraphrases it away)
- [ ] pipeline.py: Kling-specific Opus instruction in _write_motion_prompts; motion_prompts records gain "provider" + "negative_prompt"

## Phase 3 — threading, request build, cost visibility
- [ ] pipeline.py: generate_ugc_video(broll_video_provider=BROLL_VIDEO_PROVIDER) validated BEFORE any client is built (same block as _LIPSYNC_ENGINES/_LLM_PROVIDERS)
- [ ] pipeline.py: thread the provider into BOTH stage 5b (_write_motion_prompts/_fallback_prompt) and the clip fan-out (_clip_request Kling branch); _requested_duration unchanged (Kling range 3–15)
- [ ] pipeline.py: record the provider used per clip in manifest["clips"]; log a per-run cost estimate split by provider (13.5 vs 5.4 cr/s, $0.014722/cr) + delta vs all-Seedance

## Phase 4 — resume the two manifest bugs
- [ ] pipeline.py: _Manifest.get(key, default) — the class never had one, so :1637's hasattr guard was permanently False → every talking head re-lipsynced every run
- [ ] pipeline.py: _Manifest.load(path) classmethod (rehydrate _data, keep version, raise on missing/corrupt — __init__ always built a fresh dict)
- [ ] pipeline.py:1637 → plain manifest.get("lipsync"); delete the dead hasattr guard
- [ ] pipeline.py: resumable_runs(jobs_root) — scan */manifest.json without final.mp4; per beat report local-clip / paid-task-id / missing

## Phase 5 — resume_ugc_video (tiers) + UI
- [ ] pipeline.py: resume_ugc_video(run_dir, ...) rehydrating beats/windows/anchors/motion_prompts/VO/beat audio/alignments/refs/voice/params
- [ ] tier 1 valid local clip-N.mp4 (ffprobe dur>0) → reuse FREE; tier 2 clip_task_ids[N] completed AND range-GET 2xx → download FREE; tier 3 → re-order PAID. LOCAL-FIRST (3 of 5 stranded runs have clip_task_ids:{} but clips on disk)
- [ ] tier 2 MUST range-GET: /v1/tasks/{id} keeps saying "completed" long after the artifact 404s (~72h retention)
- [ ] resume is idempotent — safe to call twice; log the tier per beat + total PAID cost
- [ ] ui.py: "B-roll video provider" radio next to clip_resolution (Seedance / Kling v3 ~2.5x cheaper), threaded component → gen_btn.click inputs → on_generate_v2 positional → kwarg (inputs order and handler signature edited together)
- [ ] ui.py: resumable-run dropdown ("v2-20260714-131011  (10/11 clips paid)") + "Preview resume" showing per-beat tiers AND cost before committing + "Resume run"; refresh without an app restart

## Phase 6 — verification
- [ ] Unit (ALL MOCKED, zero live calls): provider_for_model; _seedance_body byte-identical across the existing matrix; _kling_body key allow-list + audio_url ValueError; _kling_quality; _use_kling truth table incl. background_vo+native→Seedance; _kling_broll_prompt cap/truncation/camera-strip/tail-exactly-once/negative selection; alias identity; _Manifest.get/load; the :1637 prior_lipsync_records regression; resumable_runs classification; tier selection + 404 fallthrough + idempotence + cost log
- [ ] `python -m pytest tests/ -x -q` green (424 baseline)
- [ ] LIVE VALIDATION IS CREDIT-GATED (~$100 left — needs an explicit top-up + go-ahead): (1) resume-assemble v2-20260702-153827, must cost $0 (10/10 tier 1); (2) resume v2-20260714-131011 (10 free / 1 paid, exercises tiers 1+3 and tier 2 if <72h old); (3) one short all-Kling-b-roll 720p run A/B'd against all-Seedance — mid-clip cuts, frozen reaction beats, label/size drift, skin plasticity, logged cost delta
