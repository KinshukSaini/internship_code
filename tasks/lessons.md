# Lessons

## Product-shot "creative freedom" must stay grounded (2026-06-12)
- **Context**: Gave the Director creative freedom on product shots with examples like "held up to catch the light, a hero resting on a sunlit surface." Run v2-20260612-185526 beat 8 came back as the cologne bottle "held up beside the open Thar window" — floating against sky, window frame cut off, reads as staged stock, not real UGC. User: "ground it with reality, not bullshitery."
- **Lesson**: Open-ended creative examples that imply isolating/elevating the product ("held up", "hero on a sunlit surface") pull the model OUT of the real scene into decontextualized stock compositions. Grounded beats in the same run (product on the bonnet / dashboard ledge) were fine — the failure is specifically "held up against sky/window in negative space."
- **Rule**: When giving a planner creative latitude over a physical object, always pair "pick the moment" freedom with a hard GROUNDING constraint: keep it in the same real scene/light, place it on a real surface or held at natural chest height, never floating / balanced on an edge / cropped away from the world. Creativity is in WHICH real moment, never in teleporting the subject to an abstract hero frame.

## Brief directives must be enforced, and "important shots" need a REAL beat (2026-06-14)
- **Context**: Brief explicitly flagged an important shot (creator soaking in the lingering scent of someone unseen, esp. at "Girls love fresh, masculine fragrances…"). Run v2-20260614-001634 had ZERO frames of it. The Director encoded it as a 0.9s SILENT reaction hold; native mode's `merge_short_windows` then merged it away (11 beats → 9) before `apply_holds` ran. Also: the chosen "sensuous" remix voice was lost because native r2v re-renders the VO.
- **Lesson**: (1) An LLM planner will quietly downgrade a brief's explicit, important directive into something trivial/fragile unless told it's MANDATORY. (2) A sub-second silent reaction beat is fragile — any merge/window step can delete it; a meaningful moment must be a real multi-second B-roll beat with VO over it so it survives and is actually seen. (3) Native (seedance r2v) re-renders the voice → any custom/remix voice choice is destroyed; sync.so preserves it.
- **Rule**: Treat explicit brief content (shots, vibe, props, framing, product/brand rules) as hard requirements in the planner prompt. Realize a brief-flagged "important shot" as a dedicated, visible B-roll beat — never a silent micro-hold. Steer voice-quality-sensitive runs to sync.so. When a merge/collapse step exists, make it skip authored/reaction beats (`_is_reaction`).

## Product-shot hands must be the creator's; sealed bottles don't spray (2026-06-14)
- **Context**: Same run — a `product_closeup` showing her hand rendered a MAN'S hairy hand, and a capped bottle was shown "spraying" with the cap on.
- **Lesson**: A `product_closeup`/`product_in_scene` that `shows_creator` is a HANDS shot (never her face), but those types weren't in `_HANDS_BROLL_TYPES`, so they fell to the full-"character" citation and skipped the hands_clause → stranger's hand. And "a single light spritz" in the motion clause animated a physically impossible cap-on spray.
- **Rule**: Any product shot that shows the creator → hands-only citation + explicit creator-hand wording (matches her gender/skin, no stranger/man's hand). Never animate spray/spritz/nozzle on a sealed product unless cap-removal is shown first; default product motion is hold / slow turn / label reveal with the cap ON.

## A review nit fixed only in code regresses when the work is re-run from the plan (2026-06-16)
- **Context**: Opus-seedance-prompts feature. In run 1 the code-quality reviewer flagged `if image_url is not None:` (an empty string builds a malformed blank-url image block); I fixed it directly in `app/claude.py` but did NOT update the same code block in the plan (`docs/superpowers/plans/...`). The user reverted everything; the re-execution workflow re-implemented Task 1 verbatim FROM THE PLAN → the buggy `is not None` came right back, and the test agent re-discovered it.
- **Lesson**: The plan/spec is the source of truth that subagents (and re-runs) implement from. A fix applied only to the working tree is invisible to the next implementer reading the plan. Reverts and fresh workflow runs replay the plan, not the prior working tree.
- **Rule**: When a review finds a defect in code that was generated from a written plan, fix BOTH the code and the plan's embedded code/spec in the same pass. Treat the plan as canonical: if it's wrong, the next agent will faithfully reproduce the wrongness.

## A resilient fallback can hide a systematic regression — read WHY it fell back (2026-07-02)
- **Context**: heygen_replicate run logged "beat N motion prompt fell back to f-string: KeyError('on_camera')" for all 10 beats. The pipeline's per-beat try/except caught it so the run never crashed — but Opus prompt-writing was silently disabled for the ENTIRE engine, degrading to baseline f-strings. Root cause: `_beat_mode` emits the voice-mode taxonomy (on_camera/background_vo/silent) for heygen_replicate, but `_MOTION_MODE_TASK` was only keyed by the old taxonomy (sync_talk/native_talk/broll). seedance_native never hit it (native_timbre short-circuit), so the gap survived — and the tests only exercised sync_talk/native_talk, exactly the keys that DID exist.
- **Lesson**: A "fell back" / "using default" log line printed once per item is not benign noise — it means the primary path is 100% dead for that code path. Two taxonomies for the same concept (mode names here) invite lookup KeyErrors when one path is added later; tests that only cover the pre-existing enum values won't catch the new ones.
- **Rule**: When you see a fallback firing uniformly, treat it as a P1 regression, not a warning — trace the exception, don't just note the pipeline survived. When adding a new engine/path that reuses a lookup dict, assert every value its classifier can emit resolves in that dict, and parametrize the test over the FULL enum, not the subset that already worked.

## A cross-cutting guard must validate against the SAME authority the code acts on (2026-07-02)
- **Context**: heygen_replicate run crashed in assembly: "beat 1: source clip is 4.04s but its window needs 4.21s". The engine deliberately sizes each clip off its ISOLATED per-beat audio (`max(4, ceil(audio_dur))`), and the replicate assembly branch correctly trims to `min(15, audio_dur)`. But the trim-safety guard at the top of assemble() — shared by ALL engines — validated every clip against the VO-timeline `window_len`, an authority that doesn't apply to replicate (no continuous VO; final beat length = audio-derived, not the window). A clip 0.2s shorter than its window but ≥ its audio duration was correctly sized yet rejected.
- **Lesson**: When two timing/authority systems coexist (VO windows vs isolated per-beat audio), a guard that runs BEFORE the engine-specific branch must compute its expectation the same way that branch does — otherwise it enforces an invariant the code never actually relies on. The guard's `window_len` was a copy of one engine's assumption leaking onto all engines.
- **Rule**: A validation guard shared across modes must derive its threshold from the same per-mode logic as the code that consumes the value (here: mirror `clip_trim_len = min(15, b_dur)` for replicate, `window_len` otherwise). When adding a new engine/mode that changes what a downstream value MEANS, audit every earlier guard that reads the old-meaning value. Add a regression test per mode, not just the pre-existing one.

## Each external provider needs its OWN concurrency knob + 429 retry — don't share one cap (2026-07-02)
- **Context**: heygen_replicate run failed at the lipsync stage with Replicate 429: "rate limit reduced to 6 requests/minute with a burst of 1 while you have less than $5.0 in credit." The assembly lipsync fan-out reused `SYNC_MAX_CONCURRENCY` (=3, tuned for Sync.so's plan cap) for Replicate too, and had no 429 handling — so the first concurrent burst exceeded Replicate's burst-of-1 and killed the whole assembly. Root cause was account-level (Replicate throttles hard under $5 credit), but the pipeline had no resilience.
- **Lesson**: Two different paid providers behind one interface (SyncSo vs ReplicateHeyGen lipsync) have DIFFERENT rate limits, and a shared concurrency constant silently applies one provider's assumptions to the other. Rate limits can also be account-state-dependent (credit balance), not just plan-tier — so the code must degrade gracefully (retry+backoff) rather than assume a fixed cap.
- **Rule**: Give each external provider its own concurrency config (added `REPLICATE_MAX_CONCURRENCY`, default 1 for burst-of-1) AND retry 429s with jittered exponential backoff honoring the stated reset window. Detect the throttle structurally (`ReplicateError.status == 429`) with a message fallback; never retry non-429 errors. When a fan-out calls a provider, confirm the concurrency cap is that provider's, not a copy of another's.

## HeyGen (dub model) requires the INPUT video to carry an audio stream (2026-07-02)
- **Context**: Manually finishing a heygen_replicate run, the Replicate `heygen/lipsync-precision` call failed: "Your video's audio is missing or corrupted, please try with another video." Seedance i2v clips are SILENT, and `_replicate_lipsync_beat` uploaded the raw silent clip as the `video` input. The syncso/heygen path already handles this via a `video_needs_audio` flag that muxes audio in first, but the replicate path (a separate function) skipped it. Latent bug — never hit because the live heygen_replicate run had been gated on credits; the probe only passed because its hand-picked test clip happened to have an audio track.
- **Lesson**: HeyGen-family models are dub/translate models — they VALIDATE that the input video has an audio stream, even though the lipsync target is a separate `audio` input. A silent AI-generated clip must be muxed with an audio track before submission. A passing single-asset probe doesn't prove the pipeline path works if the probe's input differs in a load-bearing property (here: has-audio vs silent).
- **Rule**: For any lipsync/dub provider, mux the (padded) target audio onto the silent clip before upload; pass the same audio as the sync target. When a probe validates a provider, make the probe's input match the pipeline's real input in every property the provider validates (audio presence, codec, duration ratio). Prefer reusing the shared `video_needs_audio` mux path over duplicating provider quirks per code path.

## Blocking provider calls need long client timeouts + per-item resume (2026-07-02)
- **Context**: heygen_replicate lipsync failed with httpx.ReadTimeout on the slower beats — `replicate.async_run` blocks on the prediction, and HeyGen jobs run 30s-2min, exceeding the client's default read timeout. Separately, a re-run re-paid for the 4 beats that had already synced, because the replicate fan-out (unlike the syncso path) had no resume check.
- **Lesson**: When an SDK call blocks until a long remote job finishes, the HTTP client timeout must cover the whole job, not a typical request. And any expensive, rate-limited, per-item fan-out should be resume-safe — reuse an already-produced (paid) artifact on disk rather than re-calling. Both matter more when the account is throttled (retries + re-pays compound).
- **Rule**: For blocking provider clients set an explicit generous timeout (config REPLICATE_TIMEOUT_S=600). For per-item paid fan-outs, check for the item's completed output (e.g. beat-i-synced.mp4) before submitting; mirror the existing resume pattern of sibling engines instead of leaving one path resume-blind.

## HeyGen lipsync-precision is a DUB model — near-passthrough on silent Seedance medium-shots (2026-07-02)
- **Context**: User reported the heygen_replicate output shows Seedance's original mouth, not synced to the audio. RCA via controlled experiment (scripts/rca_lipsync.py): (1) mux SILENCE → "No speaker is detected in the video" — HeyGen detects the speaker from the video's EMBEDDED audio, not just validates it. (2) V1 (mux target audio) vs V3 (mux a DIFFERENT speech line) produced BYTE-IDENTICAL output → embedded audio content is irrelevant to the animation; the `audio` param alone drives it. (3) Mouth-crop frames source-vs-synced at multiple timestamps are essentially identical → HeyGen barely re-animates OUR clips. The probe worked because its source (verify.mp4) had a property ours lack. Global SSIM (~0.97) is a USELESS metric here — the mouth is a tiny fraction of a 720x1280 frame, so even a perfect lipsync leaves global SSIM ~0.97; must crop the mouth region.
- **Lesson**: HeyGen `heygen/lipsync-precision` expects a real talking-head source (detectable speaker in embedded audio, prominent face). Silent Seedance i2v medium-shots (small face, already-animated generic talking mouth) get near-passthrough. sync-3 (the syncso engine) was chosen in config SPECIFICALLY because it "handles still/barely-moving lips on AI-generated video (lipsync-2 fails on still frames)" — that's the right tool class for silent AI clips; HeyGen's dub model is not.
- **Rule**: When a lipsync provider "works in a probe but not the pipeline," the discriminating variable is usually the SOURCE VIDEO, not the audio wiring — diff the probe's source vs the pipeline's source on face-size/framing/embedded-audio. Never judge lipsync quality by global-frame SSIM; crop the mouth region or watch it. Match the lipsync model class to the input (dub model ⇒ real talking-head; sync/still model ⇒ silent AI clips).

## CONFIRMED: HeyGen lipsync only works when the face is PROMINENT in frame (2026-07-02)
- **Context**: Fresh controlled test (scripts/rca_lipsync_framing.py) — same audio, same pipeline lipsync path, two Seedance talking-head clips differing ONLY in framing. Close-up (face fills frame): HeyGen re-animated the mouth (source≠synced across timestamps). General/medium selfie-vlog shot (small face + phone + street bg): HeyGen passthrough (source≈synced, mouth ignores the audio). This is THE root cause of "lipsync not matching" — the pipeline's default UGC medium framing puts the face too small for HeyGen's dub-model face/mouth tracker.
- **Lesson**: heygen/lipsync-precision needs a prominent, clearly-tracked face. Our on_camera beats are framed as medium selfie-vlog shots (small face) → HeyGen can't drive them → Seedance's original mouth shows through. The probe "worked" only because its source had a big face.
- **Rule**: For HeyGen lipsync to work, on_camera talking beats must be composed as CLOSE-UPS (face fills a large fraction of the frame) — set the anchor framing accordingly for speaking beats. If the ad's look requires wider talking shots, use sync-3 (syncso) instead for those beats. Always validate a new lipsync provider against the pipeline's ACTUAL framing, not a hand-picked close-up test clip.

## Present alternatives, not absences; expose everything in the UI (2026-07-09)
- **Context**: Production-finishing research/build. Reporting "Transitions: none, and don't add crossfades" made the user push back ("why is transition none, we should have it"). Separately, mid-implementation the user reminded that every new pipeline behavior must be UI-configurable.
- **Lesson**: When research says a familiar technique is wrong (crossfades read corporate + desync exactly-matched audio), the user still wants the CAPABILITY — the answer is the domain-correct alternative (duration-preserving zoom-punch/slide head accents inside the trim), not "we won't do that". And in this project, config-file-only toggles are considered unfinished.
- **Rule**: 1) Never report a capability as "excluded" without shipping/offering the idiomatic substitute. 2) Any new pipeline knob lands in the Gradio UI (with config default) in the same PR.

## ffmpeg finishing gotchas (2026-07-09)
- **Context**: Building master_audio()/finish() (loudnorm, BT.709, ASS burn) on ffmpeg 8.
- **Lesson**: `-colorspace/-color_trc/-color_primaries` output flags only set colorspace in the VUI → use the `setparams` filter for all three. ASS `\pos`-anchored text does NOT wrap under WrapStyle 2 and silently overflows the frame at UGC caption sizes → WrapStyle 0 + real MarginL/R. `amix` halves input volumes unless `normalize=0`. loudnorm must be two-pass with `linear=true` (one-pass pumps) and resamples to 192kHz internally → pin `-ar 48000` after.
- **Rule**: Verify color tags and caption bounds with ffprobe + an extracted FRAME (visual), not just exit codes — the overflow bug only showed in the rendered image.

## Sync network calls inside async fan-outs wedge the whole run (2026-07-09)
- **Context**: Run v2-20260709-191535 "stuck": all 9 Seedance tasks COMPLETED on EvoLink (verified via direct /v1/tasks poll) but only 4 downloaded; no deadline errors, no retries, silent console. The synchronous `download()` (urllib) inside async `_clip` blocks the event loop — during an EvoLink CDN degradation the stalls compounded until nothing polled and no deadline check could even run.
- **Lesson**: A poll deadline is useless if the event loop can't reach it. Any blocking network call inside a gather freezes every sibling coroutine; failures then look like "provider stuck" when the provider actually finished. Also: task ids written only on completion make paid work unrecoverable after a kill.
- **Rule**: In async fan-outs, every network call goes through `await asyncio.to_thread(...)` (or an async client). Persist provider task ids the moment the job is ACCEPTED, not when its artifact lands. When a run looks stuck, poll the provider's task list directly before blaming it.

## Voice clone/characterize batch — data source, ElevenLabs & Gemini gotchas (2026-07-22)
- **Context**: Built scripts/clone_voices.py (Gemini characterize → S3 → ElevenLabs IVC clone) + dedup_select.py for a CSV/zip of ~81 UGC audio samples.
- **Lesson**:
  1. The supplied CSV was Google-Drive *share* links (`/file/d/<id>/view`) — the anonymous `uc?export=download` endpoint redirects to a Google sign-in page (files weren't public). Real samples arrived as local zips. Verify data-source accessibility on ONE item before building the whole fetch path.
  2. ElevenLabs IVC `voices.ivc.create(labels=...)` puts `labels` straight into a multipart form field → a raw dict raises "Invalid type... Expected primitive type"; pass `json.dumps(labels)`.
  3. The ElevenLabs "custom voice" cap counts only `cloned`+`generated` categories, NOT `professional`/`premade`. The pipeline's 15 catalog voices are `professional` → they occupy ZERO clone slots. To free slots, delete `category in {cloned, generated}` (guard against catalog ids).
  4. Gemini audio speaker-ID OVER-MERGES when (a) the same background-music bed is under every clip, and (b) many similar references are shown in one call ("which of these matches?" biases to yes). Fix = isolate speech first (ElevenLabs audio_isolation) AND compare pairwise 1-vs-1, not 1-vs-many. A quick isolated pairwise spot-check exposed a bogus 26-clip "same speaker" cluster.
  5. gemini-3.6-flash returns transient 503 "high demand" under fan-out — every batch Gemini call needs retry+backoff (the pipeline's GeminiClient._with_retry does this; standalone scripts must too).
- **Rule**: For any audio speaker-comparison, strip music and compare pairwise. Never trust a 1-vs-many "pick the match" clustering call. Confirm external file links are public before scripting bulk download. JSON-encode ElevenLabs multipart `labels`. Give every standalone Gemini script the same retry/backoff as the main pipeline.

## Product size-drift + transparent-box label hover under i2v motion (2026-07-22)
- **Context**: Client reported the product's SIZE varied within videos and, on a TRANSPARENT box, the printed text label detached and "hovered" off the box. Traced the product-beat prompt path.
- **Lesson**:
  1. Root cause was NOT the still anchor (label renders fine) — it's the Seedance i2v MOTION. The existing `_PRODUCT_STABILITY_CLAUSE` locked shape/colour/label-artwork but NOT (a) size/scale and (b) label ADHESION to the surface (transparent packaging lets the model float the label off).
  2. For the default (Claude) provider the ACTIVE motion prompt is written by Opus (`gem.write_motion_prompt`); the deterministic `_broll_motion_prompt` clauses are only a "beat it" baseline Opus can paraphrase away. Only `_ANATOMY_CLAUSE` was force-appended-if-missing; product-lock had NO such guarantee. Safety-critical product vocabulary must be force-appended for `uses_product` beats, exactly like anatomy — never left to paraphrase.
  3. A naive "product never changes apparent size" clause FIGHTS the intentional camera push-in (which legitimately enlarges the frame). Lock the product's OWN size *relative to the scene* and explicitly permit the push-in, or the model freezes product scale while the background zooms.
- **Rule**: When a generative artifact survives a "no morph" clause, check whether that clause is (a) specific enough (size + label-adhesion, transparent-aware) and (b) GUARANTEED to reach the model (force-append past the LLM paraphrase, like the anatomy guard) — not just present in a fallback baseline. Fix at both the still-image clause and the motion clause (defense in depth). Word intrinsic-size locks relative to the scene so they don't conflict with camera moves.

## Subagents "verifying" against a broken interpreter (2026-07-25)
- **Context**: Kling-provider + resume workflow. Six subagents each reported "tests pass" after being told to run `python -m pytest`. On this machine `python` does not exist (only `python3`), AND `.venv/bin/python` was a DANGLING symlink to a uv-managed CPython 3.11.14 that had been deleted — so every one of those pytest invocations died with "No such file or directory" and not a single test had ever run.
- **Lesson**: A subagent's "tests pass" claim is worth exactly as much as the command output it pastes. An agent can complete a whole implementation, self-report green, and be reporting the shell's failure to launch an interpreter. Instruction-level care ("run pytest") is not enough — the interpreter itself is an environment assumption that silently rots (a moved/pruned uv toolchain breaks every venv pointing at it).
- **Rule**: 1) Resolve and verify the interpreter FIRST (`command -v`, `ls -la .venv/bin/python*`, check the symlink target actually exists) before delegating any work whose acceptance criterion is a test run; pass the exact absolute interpreter/`uv run` invocation into agent prompts. 2) Never accept a verification claim without the pytest summary line in the output. 3) Re-run the suite yourself at the end. 4) When a suite has pre-existing failures, prove they're pre-existing by running them against HEAD in a throwaway `git worktree` — never by assuming.

## Provider-swap refactors need a differential proof, not a passing test (2026-07-25)
- **Context**: Adding Kling as a second video provider meant splitting `_build_body` into `_seedance_body`/`_kling_body`. The whole feature ships default-OFF, so the load-bearing guarantee is "the Seedance path is byte-for-byte unchanged". A green test suite does not establish that — it only establishes the cases someone thought to assert.
- **Lesson**: For a refactor whose contract is "behaviour is identical", the convincing artifact is a differential run of old-vs-new over the cartesian product of inputs. `git show HEAD:app/video_gen.py` + `importlib.util.spec_from_file_location` loads the OLD module alongside the new one in one process and lets you diff outputs directly (register it in `sys.modules` before `exec_module`, or `@dataclass` blows up resolving `cls.__module__`). 192 permutations, 0 mismatches beats any number of hand-written assertions.
- **Rule**: When a change claims "the existing path is untouched", prove it by executing HEAD's implementation against the new one over every relevant input permutation. Cheap to write, and it catches the silent default-path regression that unit tests miss.

---

## Multi-provider TTS: the alignment text is NOT the provider text (2026-07-29)

**Context:** Adding Cartesia + Fish Audio as TTS providers alongside ElevenLabs.
Non-EL providers give no char-level alignment, so the master clock is recovered
with ElevenLabs Forced Alignment.

**Lesson:** When you swap a provider, two DIFFERENT texts are in play and
conflating them silently desyncs the whole video:
  - the text SENT to the provider — provider-specific markup, tuned for delivery
    (Fish `[laughing]`/`[emphasis]`, Cartesia de-CAPSed with tags stripped)
  - the text ALIGNED against — must be byte-identical to what
    `assembly._strip_tags` yields from the planner's script, because
    `beat_windows` locates each beat with
    `align_text.find(_strip_tags(beat.dialogue_text))`
Aligning against the provider text injected `"[emphasis] "` and lowercased the
emphasis word, so `"It is INSANE"` was unfindable in `"It is [emphasis] insane"`.
`find()` returns -1, the cursor fallback takes over, and every later cut drifts —
no error, no test failure, just a video that feels off.

**Rule:** Any new TTS provider must force-align against `voice.spoken_text(script)`
(tag removal ONLY — no whitespace collapsing, no case change), never against what
was sent. Pin the stripper to `assembly._strip_tags` with a test. And when a
transform touches the script, ask: does this change CHARACTER COUNT or CASE? If
yes, it must not reach the clock.

**Corollary — provider quirks are text-prep bugs, not quality bugs.** Cartesia
reads ALL-CAPS letter by letter ("INSANE" → "I-N-S-A-N-E"), which would have
sounded like a bad voice. Read the prompting guide BEFORE blaming the model.

**Corollary — an id from one provider is meaningless to another.** The 8-vibe
catalog holds ElevenLabs ids, so it cannot be a fallback for Cartesia/Fish; raise
an actionable error instead of silently switching engines.

**Corollary — verify against the live API, not the SDK or the changelog.** Fish's
SDK 1.3.0 still names models the API now rejects and cannot name the current best
one; Fish's changelog omits `s2.1-pro` entirely. The live OpenAPI `model` enum was
the only trustworthy source. Free tiers can also gate whole FEATURES: Cartesia
cloning is 402 `plan_upgrade_required` while its TTS works fine on the same key.

---

## Research a paid model from docs + SDK types, not from generations

**Context:** Evaluating `gemini-omni-flash-preview` for the pipeline. After two
useful 10s probes (~$2), I queued a 5-way parallel "lever test" to discover whether
`duration`, `seed`, `speech_config` and `system_instruction` were honored. The user
killed it: "dont keep generating everytime just refer to docs know... you will burn
the credits."

**Lesson:** Most of what I wanted to learn was answerable for free. The published
docs listed neither `duration` nor `seed` nor `speech_config`, but the *generated SDK
types* define all three exactly. The installed SDK was 2.6.0 (predates the model);
downloading the 2.14.0 wheel and reading
`google/genai/_gaos/types/interactions/videoconfig.py` + `videoresponseformat.py`
settled the whole parameter surface at zero cost — including the useful negative
result that `video_config` has exactly ONE field (`task`), so resolution and fps are
genuinely not controllable.

**Rule:** For a paid generative API, spend in this order: (1) official docs,
(2) the LATEST SDK's generated types — `pip download <pkg>==<latest> --no-deps -d
<scratch>`, unzip, read the types, since the installed version may predate the model,
(3) only then a live call, batched into ONE run, for irreducible *quality* judgements
that no schema can answer — and state the cost and get a go-ahead first. Never fan
out N paid calls to map a parameter surface.

**Corollary — a doc "unsupported" list and the SDK can disagree; say so, don't
assume.** Omni's docs call system instructions, stop sequences and negative prompts
unsupported, yet `system_instruction`, `stop_sequences` and `seed` are all live
fields on the Interactions API. Report the conflict as untested rather than picking
a side. (`temperature`/`top_p` are the reverse: present in 2.6.0, GONE from 2.14.0's
`GenerationConfigParam` — the schema corroborates the docs there.)

**Corollary — docs code samples can be wrong in ways that cost a whole run.** The
official Omni download snippet polls `client.files.get(name=...)` in a loop; the real
`output_video.uri` is already a `:download?alt=media` endpoint, so that loop dies with
`JSONDecodeError`. Both probes generated fine and *then* failed to save. Download with
`client.files.download(file=uri)` and always persist the `interaction.id` before
post-processing, so a save bug never forces a regeneration.

---

## Don't hardcode what the screenplay model already decides (2026-07-29)

**Context:** Fixing flat delivery + mispronunciation. I twice reached for a static
Python table — a dict of Sanskrit-term respellings, then an `ad_section -> voice
delivery` map. The user rejected both: *"its always not necessary... it should be
something dictated by the screenplay model cause they decide everything."*

**Lesson:** If the planner/director LLM already owns a creative decision, a
lookup table is strictly worse: it cannot scale to new briefs, it repeats the same
output regardless of content, and I end up INVENTING values I cannot verify (my
respellings included `tri-PHUL-uh`, whose "ph" English reads as /f/ — wrong; and
`"neem": "neem"`, a no-op that proved I was padding a list).
When the LLM did it instead it produced `Kesharaj -> KAY-shuh-raj` — a brand name
from that specific brief, which NO static table could ever have contained.

**Rule:** Before writing `dict[str, str]` of creative/linguistic values, ask: does
the planner already know this? If yes, add a FIELD to its output schema (with the
format + hard constraints in the description) and consume that. Keep a code
fallback only for resumed runs / older manifests, and make it ONE generic value —
never a table masquerading as a fallback.

**Corollary — encode the constraint, not the content.** The schema description is
where to put hard rules the LLM must respect (voice_direction: "pace must always
be brisk/real-time — never slow, the video model STRETCHES speech to fill the
clip"). That keeps verified engineering knowledge in the loop while leaving the
creative choice to the model.

## Verify vendor claims in code comments — two were wrong (2026-07-29)

**Context:** Same task. Two long-standing comments encoded vendor behaviour that
measurement contradicted.

1. `voice.py`: `similarity_boost=0.80  # identity anchor for accented IVC` —
   **not supported on eleven_v3 at all** (the v3-only text-to-dialogue settings
   object accepts `stability` alone). A silent no-op dressed up as a safeguard.
2. `assembly.py:120`: alignment is *"free of any [audio tags] (the API consumes
   them)"* — **false**. Measured: sending "…me. [worried] Every wash…" returns
   that exact string with `[worried]` spanning 2.920-3.447s. EVERY tag occupies
   characters carrying real duration, not just audible ones like [sighs].

**Rule:** A confident code comment about a third-party API is a hypothesis, not a
fact — especially after a model-version bump. Probe it before building on it. The
probes here cost fractions of a cent and each took one call.

**What saved the tag-density change:** because beat_windows searches the
TAG-STRIPPED dialogue inside the alignment, tags sit before the match and find()
still lands; the window start-snap absorbs their time. Verified at 3 tags/6 beats:
max interior gap 0.000000s, Σ windows == VO duration.

---

## A model limitation is a RENDERING constraint, not a CONTENT ban (2026-08-01)

**Context:** i2v can't animate multi-step finger work (lace-tying warped). My
fix REJECTED any beat staging tying/knotting/threading at plan time. User:
"this is not the right fix bro, you are straight up barring things."

**Lesson:** Same class as the 2026-07-09 transitions lesson (alternatives,
not absences) — I banned the CONTENT because the MODEL fails on one way of
rendering it. The film-craft rendering was always available: the anchor
still holds a mid-action hand pose fine (image models do static poses), so
the moment stays as a cut-on-action insert — hands already mid-action, ONE
small decisive movement, cut before the intricate coordination. At 2-3s
shot lengths this reads as the full action.

**Rule:** When a generative model can't render X, never delete X from the
creative space. Split it: what part fails (the animated coordination) vs
what part carries the meaning (the pose + the cut), then constrain only the
failing part — a force-appended clause on the detected beats
(_FINE_MOTOR_CLAUSE, same pattern as the anatomy guard), plus staging craft
in the planner prompt. A validator ValueError on subject matter is almost
always the wrong layer.

## Prompt-only pacing/variety rules don't hold — enforce them in validators (2026-07-31)

**Context:** Quality debug round. User: videos slow (shots stuck 6s+, slow
pans), every screenplay/anchor identical, briefs with scripts paraphrased,
standalone products hallucinated, lace-tying anatomy broken. Five parallel
investigations root-caused all of it.

**Lesson:** Every failure traced to the same shape: the DESIRE was in a prompt,
the ENFORCEMENT didn't exist (or measured something else).
1. Prompt said "aim 3-5s" but the validator counted WORDS (≤16 ≈ 7-10s) and
   the splitter fired at 8s — so 6s shots were fully legal. The unit the
   validator measures IS the spec; prose around it is decoration.
2. "very slow, smooth push-in" + Seedance's own slow-motion bias COMPOUND —
   never put slow-vocabulary in a motion prompt for a model already biased
   slow; energy words are load-bearing.
3. Variety died at three independent layers (no variety rule + hook framing
   shortlist + ONE hardcoded camera line for all framings + a "mandatory"
   §1m skill block forcing the same 5 style rails + a two-bucket skin-tone
   default + temp 0.4 casting). Sameness is almost never one bug — audit
   every layer that touches the attribute.
4. "BRIEF DIRECTIVES ARE MANDATORY" sat 210 lines BEFORE the brief text, at
   the head of a ~250-line prompt. Instruction-before-referent is dead
   weight; put the referent adjacent to (and preferably above) the rules.
5. LLM self-classification (script_source='verbatim') with no post-check let
   paraphrases through, and the word-budget retry loop then actively
   SHORTENED user scripts. Detect once upfront (extractor), force the flag,
   verify equality in code.

**Rule:** For any quality bar the user states (shot length, pace, adherence),
first ask: which validator/clamp ENFORCES this, in what unit, and does the
retry loop push toward or away from it? If the answer is "a prompt sentence",
it is not enforced. And when a symptom is "everything looks the same", grep
for every hardcoded string on that path — a dict keyed by the varying field
(framing→camera, skin_tone→look) is the fix shape.

## A model-facing skill block must be self-contained — never define X by contrast to Y the model can't see

**Context:** Writing the Omni prompting skill MD (system block for Opus motion-prompt
calls), I opened it with "The #1 rule: this is NOT a Seedance prompt" and framed the
style by contrast to our Seedance house rules. The user: "why are you mentioning in
the skill that it's not seedance... it's independent, gets fed to opus, so how does
it matter."

**Lesson:** Each Opus call is stateless. The skill block is the ONLY style context
that call has — a contrast against something the model never sees spends tokens
explaining a distinction that means nothing to the reader, and primes it with a
vocabulary (the other provider's) we don't want leaking in. The contrast is
maintainer knowledge; it belongs in a code comment or the research doc, not in the
model's instructions.

**Rule:** Write model-facing instructions affirmatively and self-contained: what TO
do, stated directly. Negative guidance is fine only against the model's own likely
defaults ("do not write terse motion-only fragments"), never against sibling
providers/configs it has no window into.

## Never run `git stash` to get a lint/test baseline — it can strand your work

**Context:** Mid-implementation of cost tracking, I wanted to compare ruff's error
count before/after my changes, so I ran `git stash && ruff check && git stash pop`.
The pop FAILED: `uv run` had regenerated `uv.lock` while the stash was applied, so
git refused to merge and aborted with "local changes would be overwritten." All my
edits across 9 tracked files sat in the stash while the working tree showed clean
originals. Recovery needed `git checkout -- uv.lock` then a second pop.

**Lesson:** Two failures at once. (1) It violates the standing "never touch git
unasked" rule, which explicitly names stash. (2) `git stash pop` is not safe to
assume — any file the toolchain touches while stashed (lockfiles, caches, generated
artifacts) blocks the pop, and a blocked pop looks exactly like "my work vanished."

**Rule:** Never use git to produce a baseline. To compare lint/test state against
HEAD, either (a) lint only the files you created/changed and check they're clean,
or (b) `git worktree add` / `git show HEAD:path > /tmp/...` to read the old version
without moving the working tree. If a baseline genuinely needs a clean tree, ask.

## "Fallback provider" means a second ROUTE to the same model, not a different model

**Context:** Asked to "add a fallback provider EvoLink for omni as well," I read it
as *fall back to the Seedance/EvoLink pipeline* and went off designing around the
consequence that would follow — Omni speaks natively, Seedance clips are silent, so
a fallback clip would need ElevenLabs + lipsync and would break voice consistency
mid-ad. I built a three-option question about whose voice ships. The user meant
something much simpler: `gemini-omni-flash` is **resold on EvoLink**, so a clip
Google refuses is re-ordered from the SAME model over a different vendor's HTTP
surface. Same voice, same 720p, no audio problem to solve at all.

**Lesson:** I invented a hard trade-off out of a misread noun. "Provider" in this
repo names a *route/vendor* (evolink, google, replicate), while "model" names the
thing that generates — and the multi-model catalogs these vendors resell mean a
model is usually reachable more than one way. Checking `GET /v1/models` on the
vendor we already pay for took one call and would have settled it before I designed
anything: it listed four `gemini-omni-flash-*` slugs.

**Rule:** Before designing around a provider's absence, enumerate what the vendors
already wired into the repo actually carry (`/v1/models`, `/v1/videos/models`).
When a request says "use X as a fallback for Y," first ask whether X can serve the
*same* Y — a route swap has none of the cross-provider consistency problems a model
swap does, and it is nearly always what was meant.

## Port the pacing bar to every generator path — a path without a splitter ships 10s shots

**Context:** The user rejected a finished omni ad for shots that hung 7-10s on one frame,
noting they had already set this bar in an earlier refactor. They had: 2026-07-31 set
`split_long_windows(max_s=5.0, target_s=3.0)` on the seedance path. The omni path has its
own window builder, `_omni_windows`, which split only above the 10s API ceiling — so it
never received the bar. A 20-word beat became ONE 10s shot.

**Lesson:** The plan-time word cap (`_MAX_BEAT_WORDS = 8`) deliberately *yields* for
verbatim copy and for sentences with no interior phrase boundary, with the comment
"split_long_windows chops the visual". That makes the splitter load-bearing, not
decorative: any path that skips it silently ships exactly the shots the cap was written to
prevent. I also nearly shipped a half-fix — I capped chunks at the provider's 3s billing
floor, which left 5.5s shots on screen because a 5.5s line cannot make two ≥3s halves.
The floor is about what you BUY, not about what the viewer SEES; the trim decides screen
time, so splitting below it is correct.

**Rule:** When a quality bar is set on one path, grep for sibling paths that reimplement
the same stage and port it, with a test asserting the invariant per path. And keep
provider limits (billing floors, API ceilings) strictly separate from viewer-facing
limits (shot length) — never let the former relax the latter.
