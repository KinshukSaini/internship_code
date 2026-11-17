"""Environment-driven settings. Loaded once at startup."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env at import time so BOTH the module-level constants below and the
# PATH override see it (load_settings() re-calls this — idempotent).
load_dotenv(override=False)

# Optional directory holding the ffmpeg/ffprobe binaries the pipeline should
# use, prepended to PATH so every subprocess call site picks them up without
# threading a binary path through 20+ command lists. Needed since the 2026
# homebrew-core ffmpeg formula dropped libass — stock `brew install ffmpeg`
# can no longer burn captions (no `subtitles` filter); point FFMPEG_DIR at a
# libass-capable build (e.g. a static build in <repo>/bin).
FFMPEG_DIR = os.environ.get("FFMPEG_DIR", "").strip()
if FFMPEG_DIR:
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

# Image-gen model IDs (Google Gemini Nano Banana family).
# "hero"  → Pro:  raw photorealism (girlfriend-selfie aesthetic).
# "anchor"→ NB2:  cheaper/faster, creator-content aesthetic, thinking_level=high.
NB_PRO_MODEL = "gemini-3-pro-image-preview"
NB2_MODEL = "gemini-3.1-flash-image-preview"
# Tried in order when the primary image model is down (5xx, e.g. 503
# "high demand"). The SDK's own retries run first; a fallback fires only
# once the primary is exhausted. Quality degrades down the chain, so the
# lite model is last-resort.
IMAGE_FALLBACK_MODELS = ("gemini-3.1-flash-image", "gemini-3.1-flash-lite-image")

# Prompt/text-intelligence model IDs (Gemini text models).
GEMINI_FAST_MODEL = "gemini-3.5-flash"
GEMINI_PRO_MODEL = "gemini-3.1-pro-preview"
GEMINI_FAST_FALLBACK = "gemini-2.5-flash"
GEMINI_PRO_FALLBACK = "gemini-2.5-pro"

# Text-LLM provider for script/prompt intelligence. "claude" is the default;
# "gemini" stays behind the toggle. Image generation (Nano Banana) is ALWAYS
# Gemini regardless of this setting.
DEFAULT_LLM_PROVIDER = "claude"

# Claude model IDs per tier. Opus 4.8 takes adaptive thinking + effort only
# (temperature is removed and 400s); Sonnet/Haiku accept temperature.
CLAUDE_PRO_MODEL = "claude-opus-4-8"
CLAUDE_FAST_MODEL = "claude-sonnet-4-6"
CLAUDE_LITE_MODEL = "claude-haiku-4-5"

# Cloned-voice library (real influencer voices cloned into ElevenLabs by
# scripts/clone_voices.py → dedup_select.py → clone_selected.py). When ON, the
# pipeline picks the cloned voice that best MATCHES the generated character
# (character-first: the brief drives the character; the voice is matched to it).
# The 8-vibe catalog stays as the FALLBACK when no cloned voice genuinely fits
# the character (e.g. an older character vs an all-early-20s clone set) or when
# this is OFF. Toggle via USE_CLONED_VOICE_LIBRARY env.
USE_CLONED_VOICE_LIBRARY = os.environ.get(
    "USE_CLONED_VOICE_LIBRARY", "1").strip() not in ("0", "false", "False", "")
VOICE_LIBRARY_PATH = os.environ.get(
    "VOICE_LIBRARY_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "voice_clones" / "voices.json"),
)

# Voiceover (ElevenLabs) + lipsync (Sync.so) model IDs.
# eleven_v3: expressive, supports inline audio tags ([excited], [laughs]).
ELEVENLABS_MODEL = "eleven_v3"

# eleven_v3 `stability` — how RESPONSIVE the model is to the script's audio tags.
# Three named modes: 0.0 Creative / 0.5 Natural / 1.0 Robust (Robust is
# explicitly "less responsive to directional prompts"). ElevenLabs: "For maximum
# expressiveness with audio tags, use Creative or Natural settings."
#
# 0.0 (Creative) is the default here because the read measured FLAT: the planner
# now directs delivery with ~1 audio tag per sentence, and Creative is the mode
# that actually acts on them. 0.5 was the safer half of that pair and made sense
# while the script carried only 1-2 tags — the two together (few tags + the less
# responsive mode) compounded into monotone.
#
# The documented risk at 0.0 is hallucination / accent drift on an accented IVC.
# If the QC fidelity gate starts flagging off-script words, raise this to 0.5
# rather than reverting the tag density — tags are the expressiveness, this is
# only the gain on them.
ELEVENLABS_STABILITY = float(os.environ.get("ELEVENLABS_STABILITY", "0.0"))

# Pronunciation repair is per-RUN, not a static table: the planner already knows
# which words in the script it just wrote are likely to be mispronounced (brand
# names, Sanskrit/Hindi terms, coined product names) and how they should sound.
# See the `pronunciations` field on VideoTimeline in app/gemini.py.
USE_PRONUNCIATION_FIX = os.environ.get(
    "USE_PRONUNCIATION_FIX", "1").strip() not in ("0", "false", "False", "")

# Max concurrent ElevenLabs TTS requests. The subscription enforces a
# per-account concurrency cap and returns HTTP 429 ("concurrent_limit_exceeded")
# when exceeded, so per-beat audio fan-out must be bounded (current plan: 5).
# Override via the ELEVENLABS_MAX_CONCURRENCY env var if the plan changes.
ELEVENLABS_MAX_CONCURRENCY = int(os.environ.get("ELEVENLABS_MAX_CONCURRENCY", "5"))

# ── Voice PROVIDER (TTS engine) ───────────────────────────────────────────────
# Which service speaks the script. The same 26 real influencer voices are cloned
# into all three (data/voice_clones/voices.json carries one id column per
# provider), so switching providers keeps the SAME voice identity — only the
# synthesis engine changes.
#
# ElevenLabs is the only provider that returns a char-level alignment alongside
# the audio. That alignment is the pipeline's MASTER CLOCK (beat_windows,
# trim_silences, apply_holds, captions all read cut boundaries from it), so for
# cartesia/fish the audio is generated by the provider and the clock is then
# recovered with ElevenLabs Forced Alignment (see AlignedVoice in app/voice.py).
# Consequence: ELEVENLABS_API_KEY stays REQUIRED on every provider.
VOICE_PROVIDER = os.environ.get("VOICE_PROVIDER", "elevenlabs").strip()
VOICE_PROVIDERS = ("elevenlabs", "cartesia", "fish")

# ── Cartesia (sonic-3.5) ─────────────────────────────────────────────────────
# sonic-3.5 is the current recommended stable model (42 languages, #1 naturalness,
# drop-in replacement for sonic-3 — same voice ids and request shape). Pinned
# snapshot alternative: "sonic-3.5-2026-05-04". Do NOT use "sonic-latest" — it
# tracks BETA and changes with no notice, which has no place in a paid-render
# pipeline. sonic-2/sonic-turbo sunset 2026-10-20.
CARTESIA_MODEL = os.environ.get("CARTESIA_MODEL", "sonic-3.5").strip()
# Required API version header. The `cartesia` SDK (3.5.1) hardcodes a STALE
# "2025-11-04", which silently downgrades errors to plain text — we override it
# so failures come back as structured JSON ({error_code, title, message, ...}).
CARTESIA_API_VERSION = os.environ.get("CARTESIA_API_VERSION", "2026-03-01").strip()
# generation_config.speed — a FLOAT in [0.6, 1.5] on sonic-3/3.5. The top-level
# `speed` (slow/normal/fast enum) is deprecated and "may not work for all
# voices". 1.0 is the natural read; unlike ElevenLabs (whose 1.0 read ~187 wpm
# and needed 1.15), sonic has not been measured as draggy here — raise this only
# after listening to a real render.
CARTESIA_SPEED = float(os.environ.get("CARTESIA_SPEED", "1.0"))
# Max concurrent Cartesia TTS requests. Their per-plan TTS concurrency is LOW
# (Free 2 / Pro 3 / Startup 5 / Scale 15) and returns HTTP 429 with
# error_code="concurrency_limited". Each in-flight HTTP request = 1 slot.
CARTESIA_MAX_CONCURRENCY = int(os.environ.get("CARTESIA_MAX_CONCURRENCY", "3"))
# Instant-clone clip limit: docs say "up to 10 seconds" (migration guide: 3-10s).
# Long pauses inside the clip get MIMICKED by the clone, so the clone script
# picks the longest continuous speech run rather than a blind head slice.
CARTESIA_CLONE_CLIP_S = float(os.environ.get("CARTESIA_CLONE_CLIP_S", "10"))

# ── Fish Audio (s2.1-pro) ────────────────────────────────────────────────────
# s2.1-pro is the current recommended production model: 83 languages (vs s1's
# 13 — materially better for Hinglish), open-ended [bracket] emotion tags, and
# the only tier with TTFA/DPA guarantees. Verified against the live OpenAPI
# `model` header enum (s1 | s2-pro | s2.1-pro | s2.1-pro-free) on 2026-07-29 —
# an id outside that enum is rejected 422.
#
# DEFAULT IS THE FREE TIER, deliberately. "s2.1-pro-free" is the SAME MODEL
# WEIGHTS and the same 83-language coverage as "s2.1-pro" at $0 — the only
# difference is no TTFA/DPA guarantee. Two reasons it is the default:
#   1. Measured 2026-07-29: this account 402s on "s2.1-pro" ("Insufficient API
#      credit"; Fish API credit is separate from platform credit) while
#      "s2.1-pro-free" returns audio. Defaulting to paid would ship a feature
#      that cannot run.
#   2. This pipeline is BATCH — nothing waits on first-audio-byte — so the
#      latency SLA is a guarantee we would pay for and never consume.
# Set FISH_MODEL=s2.1-pro once API credit is topped up and throughput matters.
FISH_MODEL = os.environ.get("FISH_MODEL", "s2.1-pro-free").strip()
FISH_API_BASE = os.environ.get("FISH_API_BASE", "https://api.fish.audio").strip()
# prosody.speed — float, valid range 0.5-2.0 (a post-hoc time-stretch, so it
# changes pacing without re-generating; it does NOT change phrasing).
FISH_SPEED = float(os.environ.get("FISH_SPEED", "1.0"))
# Fish gates on CONCURRENT requests, not RPM: Starter (<$100 paid) = 5,
# Elevated = 15, High Volume = 50. 429 → back off; 402 = out of credit.
FISH_MAX_CONCURRENCY = int(os.environ.get("FISH_MAX_CONCURRENCY", "5"))
# Clone reference audio: docs recommend 10-30s of clear single-speaker speech.
FISH_CLONE_CLIP_S = float(os.environ.get("FISH_CLONE_CLIP_S", "25"))
# sync-3: handles still/barely-moving lips on AI-generated video (lipsync-2
# fails on still frames). Safest for Seedance output.
SYNC_MODEL = "sync-3"
# Max concurrent Sync.so generations. The plan-level concurrency cap returns
# HTTP 429 ("Concurrency limit reached") when exceeded, so the lipsync fan-out
# must be bounded to the plan's limit (current plan: 3). Override via the
# SYNC_MAX_CONCURRENCY env var if the plan changes.
SYNC_MAX_CONCURRENCY = int(os.environ.get("SYNC_MAX_CONCURRENCY", "3"))

# Max concurrent Replicate (heygen_replicate) lipsync predictions. Replicate's
# rate limit is separate from Sync.so's and account-credit-dependent: accounts
# under $5 credit are throttled to 6 requests/minute with a BURST OF 1, so the
# default is 1 (funded accounts can raise it). Paired with 429 retry+backoff in
# ReplicateHeyGenLipsync.lipsync. Override via REPLICATE_MAX_CONCURRENCY.
REPLICATE_MAX_CONCURRENCY = int(os.environ.get("REPLICATE_MAX_CONCURRENCY", "1"))

# HTTP timeout (seconds) for the Replicate client. async_run blocks on the
# prediction, and HeyGen lipsync jobs routinely run 30s-2min — the httpx
# default read timeout trips mid-job (httpx.ReadTimeout). 600s is generous
# headroom. Override via REPLICATE_TIMEOUT_S.
REPLICATE_TIMEOUT_S = int(os.environ.get("REPLICATE_TIMEOUT_S", "600"))

# Sync.so lipsync granularity (syncso engine only):
#   "per_clip" (default) → ISOLATED per-beat audio (same topology as
#       heygen_replicate): each speaking beat gets its own ElevenLabs TTS, the
#       audio drives the clip duration (max(4, min(15, ceil(audio)))), the
#       audio is padded to exactly the clip length, each beat is lipsynced
#       against ITS OWN audio, and the synced audio stays baked in — no
#       continuous-VO overlay, no cross-clip A/V drift. Only speaking beats
#       are billed; silent b-roll/reaction beats never touch Sync.so.
#   "whole" → concat ALL beats into the silent final cut FIRST, then send that single
#       video + the FULL continuous VO to Sync.so in ONE pass (the synced output
#       already carries the VO, so there is no per-clip overlay). EXPERIMENTAL:
#       sync-3 has no "skip this segment" flag, so b-roll/reaction beats are also
#       driven by the audio (a silent reaction beat will be forced to "speak") AND
#       you pay per second for their footage too. Use for A/B comparison, not as
#       a default.
SYNC_LIPSYNC_GRANULARITY = os.environ.get(
    "SYNC_LIPSYNC_GRANULARITY", "per_clip").strip()
SYNC_GRANULARITIES = ("per_clip", "whole")

# Talking-head lipsync route. "syncso" = post-hoc Sync.so pass over a silent
# Seedance clip; "heygen" = post-hoc HeyGen /v3/lipsyncs pass over the same
# silent clip; "seedance_native" = timbre-anchored native voice (r2v).
#
# seedance_native timbre semantics:
#   - One ElevenLabs VO is generated as an ORACLE / timbre reference and fed
#     to Seedance r2v (reference-to-video). The model re-renders the speech
#     natively, preserving the voice timbre, pacing, and Indian-English accent.
#   - The oracle ElevenLabs audio is NEVER muxed into the output — it is only
#     a voice-anchor for the generation. The re-rendered speech baked into each
#     clip IS the final audio for that beat.
#   - Speaking beats (on_camera talking heads and background_vo b-roll with
#     dialogue) arrive with the re-rendered speech baked in; the assembly keeps
#     their native audio via trim_keep_audio.
#   - Silent beats (reaction holds, b-roll with no dialogue) receive a
#     synthetic silent aac track (attach_silence) so the concat can stream-copy
#     the full timeline without a codec mismatch.
DEFAULT_LIPSYNC_ENGINE = "syncso"

# HeyGen post-hoc lipsync (bring-your-own video + audio → re-rendered mouth).
# We pass the already-uploaded silent clip + VO-slice URLs straight to
# POST /v3/lipsyncs (no HeyGen asset upload). "precision" gives the best lip
# fidelity (~$0.067/s); "speed" is ~$0.033/s. Only short talking clips are sent,
# so precision is a few cents/video. Flip via HEYGEN_LIPSYNC_MODE env.
HEYGEN_API_BASE = "https://api.heygen.com"
HEYGEN_LIPSYNC_MODE = os.environ.get("HEYGEN_LIPSYNC_MODE", "precision").strip()
# HeyGen lipsync hosted on Replicate (official heygen/* models). The "precision"
# tier is a SEPARATE model slug (there is no mode param on Replicate); inputs are
# video + audio (+ dynamic_duration / speech_enhancement). Needs REPLICATE_API_TOKEN.
# Used by the "heygen_replicate" engine: per-clip ISOLATED audio, one dialogue
# line per clip, lip-synced independently (no shared-VO slicing → no A/V drift).
HEYGEN_REPLICATE_MODEL = os.environ.get(
    "HEYGEN_REPLICATE_MODEL", "heygen/lipsync-precision").strip()
# Native lipsync runs on reference-to-video — the ONLY EvoLink Seedance model
# that accepts audio_urls (i2v hard-rejects audio input with HTTP 400; gate
# probe 2026-06). The returned speech is RE-RENDERED, not a sample-exact mux:
# same words and voice, but ~±0.3s of internal re-timing. r2v duration is 4-15s.
NATIVE_LIPSYNC_MODEL = "seedance-2.0-reference-to-video"

# Seedance clip resolution (the EvoLink `quality` value). A run encodes every
# clip at one resolution — the assembly re-encode dims follow it, so 1080p is
# genuinely 1080p end-to-end (no downscale). 720p is the default (faster/cheaper);
# 1080p is opt-in for a hero render. The standard i2v model supports 1080p; the
# fast model caps at 720p (v2 uses the standard model).
DEFAULT_CLIP_RESOLUTION = "720p"
CLIP_RESOLUTIONS = ("480p", "720p", "1080p")

# B-roll video provider. Kling v3 and Seedance share the SAME EvoLink surface
# (POST /v1/videos/generations, Bearer auth, GET /v1/tasks/{id} polling) — only
# the request body and the prompt SHAPE differ, so routing is per-clip.
# Why offer Kling at all: kling-v3-image-to-video bills 5.4 credits/s ($0.0795/s)
# vs seedance-2.0's 13.5 credits/s ($0.199/s) — ~2.5x cheaper, verified from both
# the EvoLink price list and our own billing history (1 credit = $0.014722).
# "kling" routes ONLY the beats that are neither lip-synced nor audio-driven
# (b-roll / reaction / silent / background_vo); talking heads stay on Seedance
# because Kling has no audio-driven generation. Default stays "seedance" so
# today's behaviour is unchanged unless the operator opts in.
BROLL_VIDEO_PROVIDER = "seedance"
BROLL_VIDEO_PROVIDERS = ("seedance", "kling")

# End-to-end video generator. "seedance" is today's full pipeline (TTS voiceover
# as the master clock + i2v clips + post-hoc lipsync). "omni" replaces the WHOLE
# generation stage with Google gemini-omni-flash-preview (Interactions API): each
# clip arrives with model-authored speech baked in — no TTS, no lipsync, the voice
# is PROMPTED via the character's voice_persona instead of selected from a
# catalog/clone. Omni cannot accept audio input (docs/gemini-omni-research.md §4b),
# so beat timing runs on a synthetic word-budget clock and captions are recovered
# by force-aligning the script against the finished video (the seedance_native
# mechanism). Default stays "seedance"; omni is experimental (voice consistency
# across clips is a seed+verbatim-persona bet, unverified).
VIDEO_GENERATOR = "seedance"
VIDEO_GENERATORS = ("seedance", "omni")

# Omni model + knobs. 720p/24fps fixed by the API (no resolution field exists —
# generation_config.video_config has exactly one field, `task`). Duration is
# 3-10s; the response_format `duration` string is sent best-effort and dropped
# on a 400 (clips then come back 10s and the speech-end trim recovers pacing).
OMNI_VIDEO_MODEL = "gemini-omni-flash-preview"
# Synthetic clock: planned words/sec for sizing each beat's clip request. Omni
# owns its own cadence, so this only needs to be right ON AVERAGE — surplus is
# removed per clip by _detect_speech_end at assembly.
#
# Pinned to the SAME rate the planner budgets the script at (plan_timeline's
# words_per_second=2.0), so a beat's window matches the read its word count was
# written for. Our measured VO reads span 1.78-2.29 w/s, and the error is NOT
# symmetric: a window that is too long costs a little money and gets trimmed,
# while one that is too short truncates the line mid-sentence on a clip that
# cannot be repaired (Omni is the only speech source here — there is no VO or
# lipsync pass to fall back on) and can also push a beat past the 10s ceiling
# instead of splitting it. So size generously and let the trim do its job.
OMNI_WORDS_PER_SECOND = float(os.environ.get("OMNI_WORDS_PER_SECOND", "2.0"))
OMNI_DUR_MIN = 3
OMNI_DUR_MAX = 10

# ─── Omni fast-cut bar ────────────────────────────────────────────────
# UGC lives on fast cuts: a shot is 3-4s, and 5s only when it earns it. These
# mirror assembly.split_long_windows(max_s=5.0, target_s=3.0) — the user-set bar
# from 2026-07-31 — because the omni path never received it. _omni_windows split
# only above OMNI_DUR_MAX, so a 20-word beat became ONE 10s shot that hangs on a
# single frame (2026-08-05: the bread/Parle-G and stomach-crunch shots). The
# planner's own word caps (_MAX_BEAT_WORDS=8) are the pacing DRIVER; this is the
# safety net for the beats those caps deliberately let through — an unsplittable
# or verbatim line is accepted at plan time precisely because a splitter is
# expected to chop the visual afterwards.
# A SHOT may not exceed OMNI_SHOT_MAX_S; division aims at OMNI_SHOT_TARGET_S.
# These bound the shot, NOT the clip: a multi-shot take is free to run the full
# OMNI_DUR_MAX (10s) as long as it cuts every ~3s inside. That is the point of
# grouping — a long generation with fast cuts, rather than a long shot.
#
# 4.0 rather than 3.0, measured on the re-planned 2026-08-06 script: the 8-word
# planner cap yields ~3.5-4s beats, and a 3.0 ceiling divided every one of them
# into two shots that EACH paid Omni's 3s billing floor — 21 clips / $8.61 for
# the same speech that 4.0 delivers in 14 clips / $6.35, below the original
# baseline. The ceiling that matches the planner's own beat length is the cheap
# one; setting it under that length buys nothing but floor tax.
OMNI_SHOT_MAX_S = 4.0
OMNI_SHOT_TARGET_S = 3.0
# Headroom added to the CLIP REQUEST (never to the window) so a line is not cut
# off mid-sentence. Omni does NOT stretch speech to fill the duration (it speaks
# naturally and leaves trailing silence — measured: a 5-word beat finished at
# 1.53s in a 4s clip), so over-ordering is safe: assembly's speech-end trim cuts
# the surplus back out and the shot in the FINAL cut is speech-length, not
# request-length. Under-ordering is unrecoverable — there is no VO to re-cut.
#
# 2.0, not 1.0. Raised after the 2026-08-06 live run cut 3 of 24 clips off
# MID-WORD (verified by tail energy: -22 dB of full speech in the final 120 ms,
# vs -47 dB on a clip that decayed to silence). All three were the same shape —
# a 5-word SUB-SHOT fragment ("This 1 special softgel has"), 3s window, 4s
# ordered. Read time for an identical 5-word fragment ranged from 1.53s to over
# 4s: mid-clause fragments are where Omni's pacing is least predictable, because
# a fragment has no natural cadence to land on. 1s of slack did not cover that
# spread; 2s does, at ~1s of trimmed video per clip (~$0.09).
#
# The deeper fix is to stop minting mid-clause fragments at all — that needs the
# planner to author more, shorter beats so _omni_windows never has to divide one.
# See tasks/todo.md.
OMNI_SPEECH_HEADROOM_S = 2.0

# In-clip multi-shot. Omni's DEFAULT is to cut its own multi-shot mini-narrative
# (ai.google.dev/gemini-api/docs/omni), and it accepts timecode blocks —
# "[0-3s] … [3-6s] …" — to place those cuts. So a 9s beat does not need three
# 3s clips: it can be ONE generation that cuts every ~3s while the dialogue runs
# straight through. That is strictly better than chopping:
#   - the speech is rendered ONCE, so there is no seam (and nothing to truncate)
#     at a clip join, and no per-clip OMNI_DUR_MIN floor to over-buy;
#   - identity holds inside one generation far better than it does across N
#     generations each re-asserting it from a different anchor.
# What it costs: only the FIRST segment is anchored on a rendered frame, so the
# later segments are the model's own reframes — which is why product beats and
# the hook stay single-shot (see _omni_cut_count).
# Set OMNI_MULTISHOT=0 to go back to splitting long beats into separate clips.
OMNI_MULTISHOT = os.environ.get("OMNI_MULTISHOT", "1") != "0"

# How many Omni CREATE calls may be in flight at once. The clip stage gathers
# every beat with no cap, so a 24-beat run fired 24 creates in the same instant
# (2026-08-06) against a surface whose documented sibling limit is ~10 req/min.
# It happened not to 429 that time, which is luck rather than a contract: a rate
# wall here fails the clip AFTER the create may already have been accepted and
# billed. Polling is deliberately NOT capped — a poll that cannot run is a clip
# nobody is watching, and the 20-minute deadline keeps running regardless.
OMNI_MAX_CONCURRENCY = int(os.environ.get("OMNI_MAX_CONCURRENCY", "6"))
# Omni bills per output second directly (no EvoLink credits): ~$0.10/s.
OMNI_USD_PER_SECOND = 0.10

# ─── Omni's second route: the SAME model, resold on EvoLink ───────────
# Google's Interactions API is not the only way to reach gemini-omni-flash —
# EvoLink resells it on the ordinary /videos/generations + /tasks/{id} surface
# this repo already speaks (verified against EvoLink's live /v1/videos/models
# catalog on 2026-08-05: text-to-video / image-to-video / reference-to-video /
# video-edit are separate model slugs there, where Google splits the same modes
# via generation_config.video_config.task).
#
# So a clip Google refuses is not a dead clip: it can be re-ordered on EvoLink
# and come back from the same model, with the same native speech (generated
# automatically — the EvoLink route has no audio parameter either) at the same
# 720p. That is why the fallback is per-clip and silent rather than a run abort.
#
# Google stays primary regardless of price: EvoLink adds a reseller hop, its
# Omni surface exposes no seed (so cross-clip identity consistency is weaker),
# and its result links expire after 24h rather than EvoLink's usual ~72h — which
# shortens the window in which resume can recover a clip for free.
#
# Verified against evolink.ai/docs .../gemini-omni-flash-image-to-video (2026-08-05):
# body is model + prompt + image_urls (EXACTLY one) + duration (int 3-10 or
# "auto") + aspect_ratio (16:9 | 9:16 | auto) + callback_url. Nothing else —
# no resolution, no audio flag, no seed, no negative_prompt.
OMNI_EVOLINK_MODEL = "gemini-omni-flash-image-to-video"
# EvoLink's published Omni rate: video output tokens at $0.015/1K x 5792 tok/s.
# ESTIMATE ONLY, and a soft one: EvoLink bills Omni per_token and reserves ~102
# credits (~$1.50) against a 10s clip up front, refunding down to actual usage,
# so the reservation and this rate disagree by a lot. A completed task reports
# what was really charged and that always wins (see costs.price_evolink) — this
# number only prices the pre-flight estimate line.
OMNI_EVOLINK_USD_PER_SECOND = 0.087
# Set OMNI_EVOLINK_FALLBACK=0 to make an Omni clip failure fatal again (the
# pre-2026-08-05 behaviour) — useful when probing what Google is rejecting,
# because the fallback otherwise HIDES the rejection behind a delivered clip.
OMNI_EVOLINK_FALLBACK = os.environ.get("OMNI_EVOLINK_FALLBACK", "1") != "0"

# Standard (non-turbo) Kling i2v. NOT kling-v3-turbo-image-to-video: turbo drops
# negative_prompt, image_end and sound AND costs more per second — and
# negative_prompt is precisely why Kling is attractive (Seedance lacks the field,
# which is the only reason our Seedance prompts carry affirmative ban clauses).
KLING_BROLL_MODEL = "kling-v3-image-to-video"

# Kling's `quality` enum. There is NO 480p tier on Kling i2v (720p is the floor),
# so a 480p run clamps UP to 720p — still cheaper per second than Seedance 480p.
KLING_QUALITIES = ("720p", "1080p", "4k")

# Phase 2 of the v2.4 research-driven overhaul: drop chunk duration from
# 15s to 8s. Multiple sources (fal.ai gallery analysis, Wavespeed review,
# Magichour) converge on 4-8s as Seedance 2.0's quality sweet spot — past
# 8s, handheld bias jitters, walking glitches, identity drifts, and
# background morphs.
CHUNK_SECONDS = 8

# ---- Production finishing (docs/production-polish-plan-2026-07.md) --------
# Everything below runs in assembly's SINGLE finishing pass (one CRF18
# re-encode on the assembled master) + one audio-mastering step, so toggling
# features never adds encode generations.

# Burned word-pop captions (white + accent-yellow keywords, lower-middle
# third). ON by default: >80% of short-form is watched sound-off and word-pop
# captions are table stakes for paid UGC in 2026.
BURN_CAPTIONS = True
CAPTION_WORDS_PER_LINE = 3

# Subtle "real phone capture" grade inside the finishing pass: gentle
# contrast/saturation lift (BEFORE grain — grading through grain shifts the
# look), sub-pixel chromatic shift, temporal sensor grain. (No vignette —
# user explicitly doesn't want the darkened-corner look.)
# Clean AI footage lacks this signature and the ABSENCE is itself an AI tell;
# the grain also dithers away banding on flat AI gradients.
FINISHING_GRADE = True

# Finishing look applied inside the grade (recipes in app/grading.py —
# curve pivoted at mid-grey so skin brightness holds, saturation via
# skin-protecting vibrance/huesaturation): "clean_bright" is the safest UGC
# default; "warm_golden" for golden-hour briefs; "teal_orange" sparingly
# (the #1 over-grading tell); "none" = grain only.
FINISHING_LOOK = "clean_bright"
FINISHING_LOOKS = ("clean_bright", "warm_golden", "teal_orange", "none")

# Shot matching: measure each source clip's exposure/cast, correct toward
# the MEDIAN clip (tightly capped) inside the existing per-clip re-encode so
# cuts don't shift colour. Corrections are conservative by design.
COLOR_MATCH_CLIPS = True

# Gemini defect-audit of the finished ad (app/qc.py) — REPORT-ONLY, never
# blocks or edits a paid render; failures log and move on.
#
# OPT-IN (2026-08-06, user request): it is the last thing a run does, it costs a
# multimodal pass over the whole finished master, and it reports rather than
# repairs — so it only runs when the operator ticks "AI review" in the UI. This
# value is what both checkboxes initialise to, so False means unchecked by
# default; the pipeline itself is unchanged and still honours whatever it is
# handed. Set VISUAL_QC=1 to have it pre-ticked again.
VISUAL_QC = os.environ.get("VISUAL_QC", "") == "1"

# Faint pink-noise room tone mixed under the whole timeline so inter-beat
# gaps never hit digital silence (a dropout-like tell); mastered away to
# inaudible by loudnorm but keeps the noise floor alive.
ROOM_TONE = True

# Integrated-loudness target for the two-pass master. −14 LUFS / −1 dBTP is
# what Reels/TikTok/YouTube normalize toward; drop to −16 via the LOUDNORM_I
# env var if the standard target feels hot (platforms turn DOWN, never up,
# so a softer master survives as-is).
LOUDNORM_I = float(os.environ.get("LOUDNORM_I", "-14"))

# Cut-accent transition on incoming b-roll/reaction beat heads. UGC uses hard
# cuts (crossfades read corporate AND would desync the exactly-matched
# audio); the accent is a few-frame motion pop applied INSIDE the clip's trim
# so durations are preserved. "punch" = 110%→100% zoom settle, "slide" =
# 3-frame whip-in, "cut" = plain hard cut.
TRANSITION_STYLE = "punch"
TRANSITION_STYLES = ("punch", "slide", "cut")

# The FIRST product b-roll beat holds a static 12% tighter framing — the hard
# cut INTO the punch-in is the product-reveal accent. One per ad.
PUNCH_IN_ACCENT = True

# Hero-finals resolution: drafts/seed-search run at DEFAULT_CLIP_RESOLUTION
# (720p, cheap). 1080p is the standard-model-only finishing tier — it lets won
# texture survive but does NOT create realism on its own (the user confirmed
# 1080p alone stays plastic), so reach for it only AFTER the prompt fixes land.
HERO_CLIP_RESOLUTION = "1080p"


@dataclass(frozen=True)
class Settings:
    evolink_api_key: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_bucket_name: str
    aws_region: str
    gemini_api_key: str
    elevenlabs_api_key: str
    sync_api_key: str
    anthropic_api_key: str = ""
    heygen_api_key: str = ""
    replicate_api_token: str = ""
    cartesia_api_key: str = ""
    fish_api_key: str = ""
    llm_provider: str = DEFAULT_LLM_PROVIDER
    # S3-compatible object stores (Cloudflare R2, MinIO, Backblaze B2, ...).
    # Leave both blank for real AWS S3. For R2: set the endpoint to
    # https://<account_id>.r2.cloudflarestorage.com, region to "auto", and the
    # public base URL to your bucket's public host (pub-xxx.r2.dev or a custom
    # domain) since R2 has no per-bucket <bucket>.s3.<region>.amazonaws.com host.
    s3_endpoint_url: str = ""
    s3_public_base_url: str = ""


def load_settings() -> Settings:
    load_dotenv(override=False)

    evolink_api_key = os.environ.get("EVOLINK_API_KEY", "").strip()
    if not evolink_api_key:
        raise RuntimeError(
            "EVOLINK_API_KEY is not set. Add it to .env (see .env.example)."
        )

    return Settings(
        evolink_api_key=evolink_api_key,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "").strip(),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip(),
        aws_bucket_name=os.environ.get("AWS_BUCKET_NAME", "upload-data-shoppin-as1").strip(),
        aws_region=os.environ.get("AWS_REGION", "ap-south-1").strip(),
        # S3_ENDPOINT_URL falls back to AWS_ENDPOINT_URL (the name botocore
        # itself honours) so either env var works.
        s3_endpoint_url=(
            os.environ.get("S3_ENDPOINT_URL")
            or os.environ.get("AWS_ENDPOINT_URL", "")
        ).strip(),
        s3_public_base_url=os.environ.get("S3_PUBLIC_BASE_URL", "").strip(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", "").strip(),
        sync_api_key=os.environ.get("SYNC_API_KEY", "").strip(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        heygen_api_key=os.environ.get("HEYGEN_API_KEY", "").strip(),
        replicate_api_token=os.environ.get("REPLICATE_API_TOKEN", "").strip(),
        cartesia_api_key=os.environ.get("CARTESIA_API_KEY", "").strip(),
        # FISH_API_KEY is the name the official SDK reads, so honour it first;
        # FISH_AUDIO_API_KEY is accepted as the more explicit alias.
        fish_api_key=(
            os.environ.get("FISH_API_KEY")
            or os.environ.get("FISH_AUDIO_API_KEY", "")
        ).strip(),
        llm_provider=os.environ.get(
            "LLM_PROVIDER", DEFAULT_LLM_PROVIDER
        ).strip(),
    )
