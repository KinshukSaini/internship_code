# Why v2 output feels unnatural — root-cause audit + industry research (2026-06-10)

Two workflows produced this: a 37-agent code audit of the v2 pipeline (every finding
adversarially re-verified against current code) and a 105-agent web research pass on how
production AI-UGC pipelines achieve naturalness (every claim 3-vote fact-checked; 5 claims
refuted and discarded). Symptoms investigated: audio out of sync, bad lipsync, unnatural
movement, and "feels awful when clip duration exceeds natural pace."

---

## 1. TL;DR — three stacked causes

1. **Every short beat plays a truncated gesture plan.** Seedance's 4s API floor + ceil
   means a 2.0–3.0s beat window gets a 4s clip, and Seedance is never told the display
   window — it paces gestures across the full 4s, then `trim_clip` keeps the first
   50–75%. Every cut lands mid-gesture-arc. This is the user-reported "duration > natural
   pace" feeling, mechanically.
2. **The lipsync stage degrades sync three ways at once**: sync-3 receives a short trimmed
   stub instead of the full clip (a fix the repo's own docs prescribe but code never got);
   the audio slice it syncs against is shifted up to ~26 ms by mp3 input-seeking; and the
   sync.so output's duration is trusted blindly (no re-trim), so per-beat drift
   accumulates into the concat and `-shortest` silently eats the VO tail.
3. **Architecturally, mouth-only post-hoc lipsync cannot produce a natural result** —
   the body never knows what the voice is doing. Verified industry research says body
   motion decoupled from speech is THE primary AI tell (ByteDance OmniHuman-1.5 paper;
   Captions/Mirage: "the primary tell that these videos are synthetic is that the body
   language and expression doesn't align with what's being said"). The v2 architecture
   (silent I2V → post-hoc mouth sync) guarantees this decoupling. OpenShorts ships this
   exact architecture as its explicit **budget tier**; its premium tier is natively
   audio-driven (Kling Avatar v2).

---

## 2. Verified code-level root causes

### A. Stretched / unnatural pace (the "duration > natural pace" complaint)

| # | Finding | Location | Verdict |
|---|---------|----------|---------|
| A1 | `_DUR_MIN=4` floor: windows ≤3.0s always get a 4s clip; used fraction can be 20–75%. Motion calibrated by Seedance for 4s plays as a truncated fragment. | `pipeline.py:45-54` | confirmed |
| A2 | Motion prompt says "real-time speed for the full clip" but never states the display window; Seedance can't front-load arcs. Trim cuts mid-swing (e.g. 2.865s window keeps 71.6% of a 4s arc). | `pipeline.py:231-251` + `assembly.py:296` | confirmed |
| A3 | No VO pacing stage: `app/pacing.py` (silence-trim −30dB/0.3s + optional 1.15× speed-up, both in audio domain BEFORE `beat_windows`) is prescribed as P1/P2 in `docs/ai-ugc-research.md:175-176` but was never implemented. Dead air inflates every window → sluggish 1.0× feel. | `pipeline.py:411-416` | confirmed |
| A4 | B-roll `_SETTLE_CLAUSE` asks for action that "begins and completes early", but for <3s windows the settle never makes it into the trimmed region. | `pipeline.py:199-208` | confirmed |

### B. Bad lipsync

| # | Finding | Location | Verdict |
|---|---------|----------|---------|
| B1 | **Trim-before-lipsync**: sync-3 receives the already-trimmed window-length stub, not the full ceil-second clip. The repo's own roadmap (`docs/ai-ugc-research.md:178`, P2) prescribes the opposite order: lipsync the FULL clip vs a VO slice covering its span, THEN trim the output. Never implemented. | `assembly.py:278-334` | confirmed |
| B2 | `slice_audio` uses `-ss` before `-i` on the mp3 VO (input-seek): the slice content is shifted up to ~26 ms early (covers `[start-δ, end-δ]`). sync.so syncs the mouth against audio that includes the previous beat's tail. Doesn't accumulate in the final mux (VO is overlaid once), but degrades every beat's mouth timing. | `assembly.py:222-229` | partially_true (mechanism corrected) |
| B3 | End-of-beat audio tags (`"…great results [laughs]"`): tag sound occupies time after the last aligned char, which the contiguous-partition fix assigns to the NEXT beat's window — next beat's mouth syncs against `[laughs]` audio it doesn't speak. | `assembly.py:105-124` | partially_true (conditional on tag placement) |
| B4 | `LipsyncResult.output_duration` is returned by sync.so and **never read** by any caller. | `lipsync.py:87` | confirmed |

### C. Audio desync (cumulative drift)

| # | Finding | Location | Verdict |
|---|---------|----------|---------|
| C1 | **`strip_audio` has no `-t`**: whatever duration sync.so returns is re-encoded verbatim and enters the concat. Any per-beat expansion/shrinkage breaks Σclips == VO and shifts every later beat. | `assembly.py:210-219`, `:313` | confirmed |
| C2 | `trim_clip` re-encodes at `-r 24` with float `-t`: actual duration is floor-quantized to frame boundaries (~−17 ms avg/beat; up to ~375 ms over 9 beats). Concat video ends up shorter than VO → `-shortest` truncates the VO tail (CTA). | `assembly.py:198-207` | partially_true (direction corrected: video shorter, VO tail cut) |
| C3 | `-shortest` in `overlay_audio` is the silent backstop that converts any drift into truncation; the A/V drift check is warn-only and only fires above 0.5s. | `assembly.py:232-239`, `:359-368` | confirmed |

### D. Unnatural movement (beyond pacing)

| # | Finding | Location | Verdict |
|---|---------|----------|---------|
| D1 | Every beat gets an independent Nano Banana anchor; consecutive talking-head cuts teleport pose/head-angle/hands. `last_frame_url=None` is hardcoded — FLF2V continuity exists in the API layer but is never used. | `pipeline.py:465-508`, `:549` | confirmed |
| D2 | `split_long_windows` sub-beats are `copy.copy` of the parent → identical anchor prompt → the character snaps back to the same opening pose at every jump cut. (Motion prompts DO differ via index-rotated micro-gestures; anchors don't.) | `assembly.py:155-174` + `pipeline.py:444-466` | partially_true |
| D3 | `camera_fixed` is a first-class Seedance param (docs/video-gen.md:1764+) but `VideoRequest` has no such field — camera drift is uncontrolled on every clip; absence is even test-pinned. | `video_gen.py:44-55`, `:295-310` | confirmed |
| D4 | Structural: Seedance generates body motion with zero knowledge of the audio. Gesture rhythm cannot align with speech rhythm even in principle; sync-3 edits only the mouth region. | architecture | confirmed by industry research (§3) |

---

## 3. What the industry verifiably does (fact-checked, sources in workflow output)

1. **Audio-first ordering is the verified norm** — but in a stronger sense than v2's.
   Arcads' "Audio-Driven" mode feeds the finished audio INTO the video model
   ("specifically designed to prioritize synchronization between audio and visuals");
   OpenShorts feeds TTS audio into Kling Avatar v2 as a model input. Audio drives
   generation; it is not overlaid afterwards. (3-0 verified; the claim that
   Creatify/HeyGen are video-first was refuted 0-3.)
2. **The quality frontier is natively audio-driven video, not post-hoc mouth sync.**
   OmniHuman-1.5 fuses a dedicated audio branch into every DiT layer; OmniAvatar injects
   Wav2Vec2 features pixel-wise into video latents specifically so audio drives
   *holistic body motion*, not just lips. InfiniteTalk's human study: audio-driven
   full-body beats LatentSync/MuseTalk-style mouth editing ~2× on lip AND body sync
   rankings (medium confidence: author-run). Trade-off: worse identity preservation
   (CSIM 0.775 vs 0.92+) — relevant since anchor identity matters to us.
3. **Post-hoc lipsync's failures are architectural, not tunable** (3-0): masked-frame
   inpainting → lip-shape leakage, identity drift, edge blur; weak audio conditioning →
   CFG dilemma (mushy lips at low guidance, artifacts at high); per-frame diffusion →
   temporal flicker. "Precise lip-audio sync + high visual quality" was still unsolved
   in post-hoc editing as of early 2025.
4. **Never retime video to match audio.** sync.so's own docs: remap "can look unnatural";
   keep durations close upstream; "try cut_off if you are seeing drift." Fix duration at
   generation time, not in post. (v2 correctly never retimes — but it trims blind.)
5. **Long takes: streaming with context frames + keyframe anchoring**, not naive
   last-frame chaining (identity/color drift) and not first/last-frame pinning (rigid
   pose, abrupt cuts). Maps directly to D1/D2.
6. **The static-actor trade is explicit at the top end**: in Arcads' lipsync-optimized
   modes "the actor remains static (no body or camera movement)" — tight lipsync is
   bought with restrained motion, not fought with more motion.
7. **sync.so face-angle constraint** (3-0): lipsync-2 needs near-frontal faces; sync-3
   claims (vendor-only) to handle extreme angles. Off-angle handheld UGC framing is
   exactly where post-hoc lipsync degrades.
8. **Refuted/unverified — do not act on**: "Seedance 2.0 generates audio+video jointly
   in one pass" (0-3, vendor-blog sourced); Creatify/HeyGen internals (no primary
   sources survived).

---

## 4. Strategic correction to the June-2 audit

The June-2 audit REJECTED "native-audio one-shot models (Veo/Sora/Kling-native)" because
they generate their own voice (losing ElevenLabs control, Hinglish, accent fidelity), and
CUT the OmniHuman-1.5 branch as over-engineering. That conflated two different things:

- **Audio-GENERATING** models (Veo 3, Sora): make their own speech. Rejection stands.
- **Audio-DRIVEN** models (OmniHuman-1.5, Kling AI Avatar v2, InfiniteTalk, Seedance
  `audio_url` if EvoLink exposes it): consume YOUR ElevenLabs track and animate face +
  body to it. Voice control, Hinglish, accent — all preserved. This is what Arcads ships
  and what OpenShorts' premium tier uses, and it is the only known fix for D4
  (body–speech decoupling), which no amount of post-hoc patching reaches.

The already-deferred "Seedance audio_url native-lipsync experiment" is the cheapest entry
point to this class and should be un-deferred.

---

## 5. Prioritized fix plan

**Tier 1 — mechanical sync fixes (hours, no architecture change)**
1. Lipsync the FULL ceil-second clip against a VO slice covering its span; trim the
   synced output to the window afterwards (`-ss 0 -t window_len`). (B1 — already
   specified in ai-ugc-research.md:178.)
2. Re-trim after sync.so: add `-t window_len` to `strip_audio` (or a dedicated trim) and
   assert against `res.output_duration`. (C1, B4)
3. `slice_audio`: move `-ss` after `-i` (output-seek; sample-accurate on mp3). (B2)
4. Replace `-shortest` with explicit `-t {alignment.duration}` on the final mux, and
   make the drift check fail loudly above ~0.15s instead of warning at 0.5s. (C2, C3)
5. Thread `camera_fixed` through `VideoRequest`/`_build_body`; set True for static
   framings. (D3)

**Tier 2 — pacing + motion (days)**
6. Put the display window into the Seedance prompt: "complete each gesture within the
   first {window_len:.1f}s; keep talking naturally after" — so the trim lands on settled
   posture. (A1/A2)
7. Implement `app/pacing.py` silence-trim (and optional ≤1.15× audio-domain speed-up)
   BEFORE `beat_windows`, rescaling alignment timestamps. This directly attacks the
   "longer than natural pace" feel. (A3)
8. Planner-side: bias beat windows toward 3.2–4.8s so the 4s floor surplus stays ≤20%.
9. Anchor continuity for split sub-beats: give sub-beat k>0 a mid-action anchor
   description (or extract the parent clip's frame at the cut as the next anchor). (D2)
10. Restrain motion prompts: per industry finding 6, calmer body + tight lipsync beats
    lively-but-decoupled body. Drop the per-beat micro-gesture rotation on short beats.

**Tier 3 — the architectural experiment (gated, ~$10–20)**
11. Un-defer the audio-driven path: one talking-head beat rendered via (a) Seedance
    `audio_url` on EvoLink if supported, and/or (b) Kling AI Avatar v2 (image + the
    beat's VO slice) via fal/EvoLink. Compare against the current
    silent-I2V→sync-3 chain on the same beat. If body–speech coupling visibly wins,
    migrate talking-head beats; keep B-roll on the current path. This addresses the one
    cause (D4) Tiers 1–2 cannot.

---

## 6. Provenance

- Code audit: workflow `wf_318c7d1b-ed9` (6 tracers × adversarial verify, 37 agents).
  Raw findings: session task output `wntnuymc0`.
- Industry research: workflow `wf_2d71db9d-c82` (deep-research harness, 105 agents,
  3-vote verification). Raw report: session task output `wy12ocsfu`. Key sources:
  arxiv 2508.19209 (OmniHuman-1.5), 2506.18866 (OmniAvatar), 2505.21448 (OmniSync),
  2412.09262 (LatentSync), InfiniteTalk (ICLR'26 sub), sync.so official docs
  (fetched 2026-06-10), Arcads Help Center, github.com/mutonby/openshorts.
- Refuted claims are listed in §3.8 and must not be cited as fact.
