# Sync.so isolated per-beat audio — design

**Date:** 2026-07-04
**Status:** Approved
**Decision:** The `syncso` engine's `per_clip` granularity switches from
continuous-VO slicing to the isolated per-beat audio architecture already
shipped for `heygen_replicate`. The topology is generalized so both engines
share one code path; only the lipsync provider differs.

## Problem

`heygen_replicate` gets per-beat isolated ElevenLabs audio (no shared-VO
slicing → no cross-clip A/V drift), but `syncso` per_clip still slices one
continuous VO by beat window, discards the synced audio, and overlays the
full VO at the end — the drift-prone path the heygen work was built to
escape. The isolated-audio architecture is an *audio topology*, not a
HeyGen feature; sync.so should plug into it as just another provider.

## Design

### Pipeline (`app/pipeline.py`)

One predicate replaces every `lipsync_engine == "heygen_replicate"` gate on
the isolated-audio path:

```python
isolated_audio = (lipsync_engine == "heygen_replicate"
                  or (lipsync_engine == "syncso"
                      and lipsync_granularity == "per_clip"))
```

It drives:

- `resolve_voice_mode` beat routing (on_camera / background_vo / silent)
- talking-motion prompt selection (same prompts as heygen_replicate)
- per-beat ElevenLabs TTS fan-out (`beat-{i}.mp3`, bounded by
  `ELEVENLABS_MAX_CONCURRENCY`)
- audio-derived clip ordering: `max(4, min(15, ceil(audio_dur)))`
- silent i2v (no audio muxed at generation time)
- `assemble(...)` kwargs (`isolated_audio=True`, `beat_audio_paths/durs`)

Concurrency stays engine-specific: `SYNC_MAX_CONCURRENCY` for syncso,
`REPLICATE_MAX_CONCURRENCY` for heygen_replicate.

Unchanged paths:

- `granularity == "whole"`: continuous-VO whole-video sync.so pass.
- legacy direct `heygen` engine: keeps the old continuous-VO per-clip
  slicing path (`_lipsync_beat` + slice/strip/overlay).
- `seedance_native`: untouched.
- `split_long_windows` already runs for every engine, so >15s dialogue
  chunking carries over for free.

### Audio↔video mapping & length mismatch

Inherited from the heygen path, now shared:

- Mapping is structural: `beat-{i}.mp3` ↔ `clip-{i}.mp4` by index; the
  beat's own audio is the timing source for its clip.
- Clip ordered at `ceil(audio)` → video ≥ audio always, by <1s.
- `pad_audio_to` pads audio with trailing silence to exactly the clip
  duration before upload → exact 1:1 durations into sync.so;
  `sync_mode="silence"` stays as belt-and-braces.
- Post-sync `trim_keep_audio(synced, clip_dur)` normalizes length + codec;
  synced audio is baked in, concat is the final mix, no global VO overlay
  (drift check informational, as in heygen/native modes).
- Trim-safety guard (source clip shorter than required → loud error, not
  frozen frames) applies via the renamed flag.

### Assembly (`app/assembly.py`)

- Rename `replicate_lipsync` → `isolated_audio`;
  `_replicate_lipsync_beat` → `_isolated_lipsync_beat`.
- Inside `_isolated_lipsync_beat`, mux audio into the video only if
  `getattr(lipsync_client, "video_needs_audio", False)`. HeyGen models
  require an input audio track; sync.so takes the silent clip + separate
  audio directly (one less re-encode).
- Lipsync record key becomes provider-neutral (`output_url`).
- Resume (reuse `beat-{i}-synced.mp4` on disk) is already provider-agnostic.

### Sync.so config cleanup (`app/lipsync.py`, `app/config.py`)

Per current sync.so docs, sync-3 removed `temperature`,
`occlusion_detection_enabled`, and `reasoning_enabled` (expressiveness and
occlusion are handled natively). We currently send the first two:

- Drop both from `GenerationOptions`; delete `SYNC_TEMPERATURE` and
  `SYNC_OCCLUSION_DETECTION` from config.
- Keep `SYNC_MODEL = "sync-3"` (flagship, right for AI-generated faces)
  and `sync_mode="silence"`.

Sources: sync.so docs — models/sync-3, compatibility-and-tips/improving-
lip-sync-quality, models/lipsync.

### Tests

- Update `test_assembly.py` / `test_lipsync.py` for the rename and the
  dropped sync-3 options.
- New: syncso-isolated beat uploads the *silent* clip (no mux) while
  heygen_replicate still muxes; padded audio length == clip_dur; syncso
  per_clip pipeline passes `beat_audio_*` through to `assemble`.

### Error handling

Unchanged by design: per-beat lipsync failures gather with
`return_exceptions=True`; paid sync.so outputs persist to the manifest the
moment they land; resume skips already-synced beats.

## Alternatives rejected

1. Reuse the `replicate_lipsync` flag for syncso without renaming —
   smallest diff, but "replicate" flags driving sync.so is misleading in
   6+ places.
2. Provider/topology matrix abstraction — over-engineered for two engines.
3. New engine value `syncso_isolated` / env toggle — keeps the drift-prone
   legacy path alive for no user benefit.
