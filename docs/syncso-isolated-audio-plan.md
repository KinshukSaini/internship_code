# Sync.so Isolated Per-Beat Audio — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `syncso` engine's `per_clip` granularity switches from continuous-VO slicing to the isolated per-beat audio architecture already shipped for `heygen_replicate` (spec: `docs/syncso-isolated-audio-design.md`).

**Architecture:** One boolean `isolated_audio` (computed once in `generate_ugc_video`, threaded through `_beat_mode` / `_fallback_prompt` / `_write_motion_prompts`) replaces every `lipsync_engine == "heygen_replicate"` gate on the isolated path. Assembly's `replicate_lipsync` flag is renamed `isolated_audio`; the HeyGen-only "mux audio into the input video" step is gated on the client's `video_needs_audio` attribute. Dead sync-3 options (`temperature`, `occlusion_detection_enabled`) are removed per current sync.so docs.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, ffmpeg helpers in `app/assembly.py`, sync.so SDK (`sync`), Replicate SDK.

## Global Constraints

- `granularity == "whole"` (syncso) and the legacy direct `heygen` engine KEEP the continuous-VO slicing path unchanged.
- `seedance_native` engine untouched.
- Mapping invariant: `beat-{i}.mp3` ↔ `clip-{i}.mp4` by index; clip ordered at `max(4, min(15, ceil(audio_dur)))`; audio padded to exactly the clip duration before upload; post-sync `trim_keep_audio(synced, clip_dur)`.
- `SyncSoLipsync` keeps `SYNC_MODEL="sync-3"` and `sync_mode="silence"`.
- Run tests with: `python -m pytest tests/ -x -q` (full) or per-file as noted.
- Commit after every task.

---

### Task 1: lipsync.py — drop dead sync-3 options, fix `ReplicateHeyGenLipsync.video_needs_audio`

sync-3 removed `temperature` / `occlusion_detection_enabled` (handled natively — sync.so docs, models/sync-3). Also: `ReplicateHeyGenLipsync.video_needs_audio` is `False` but assembly muxes unconditionally because the model empirically rejects silent input video ("Your video's audio is missing or corrupted" — see `_replicate_lipsync_beat` docstring). Flip it to `True` so Task 2 can gate the mux on the attribute without changing heygen behavior.

**Files:**
- Modify: `app/lipsync.py` (imports ~24-25, `SyncSoLipsync` ~53-94, `ReplicateHeyGenLipsync` ~217-233)
- Modify: `app/config.py:62-69` (delete the sync-3 tuning block)
- Test: `tests/test_lipsync.py`

**Interfaces:**
- Produces: `SyncSoLipsync.lipsync(video_url, audio_url, model, sync_mode, output_file_name)` — unchanged signature, `GenerationOptions(sync_mode=...)` only. `ReplicateHeyGenLipsync.video_needs_audio == True`. `SyncSoLipsync` has NO `video_needs_audio` attribute (getattr default False in assembly).

- [x] **Step 1: Update the failing tests.** In `tests/test_lipsync.py`: replace the two option tests (~lines 50-76, `assert ckw["options"].temperature == 0.5`, `assert ckw["options"].occlusion_detection_enabled is True`, and `test_lipsync_passes_custom_temperature_and_occlusion`) with:

```python
def test_generation_options_send_only_sync_mode():
    """sync-3 removed temperature/occlusion_detection_enabled (handled
    natively) — sending them is dead config. Only sync_mode goes out."""
    # reuse the existing fake-client fixture pattern from the test above
    ...
    opts = ckw["options"]
    assert opts.sync_mode == "silence"
    assert not hasattr(opts, "temperature") or opts.temperature is None
    assert (not hasattr(opts, "occlusion_detection_enabled")
            or opts.occlusion_detection_enabled is None)
```

(Adapt to the file's existing `_FakeSyncClient` capture pattern — the assertion is: `GenerationOptions` is constructed with `sync_mode` only.) Also flip line ~214: `assert c.video_needs_audio is False` → `assert c.video_needs_audio is True`, and update its test name/docstring (the model empirically requires an input audio track; assembly muxes for it).

- [x] **Step 2: Run to verify failure.** `python -m pytest tests/test_lipsync.py -x -q` → FAIL (temperature still sent / attribute still False).

- [x] **Step 3: Implement.** In `app/lipsync.py`:
  - Remove `SYNC_OCCLUSION_DETECTION` and `SYNC_TEMPERATURE` from the `app.config` import.
  - `SyncSoLipsync.__init__`: drop the `temperature` and `occlusion_detection` params and the `self._temperature` / `self._occlusion` assignments; update the class comment ("sync-3 manages expressiveness and occlusion natively — no tuning options").
  - `SyncSoLipsync.lipsync`: `options=GenerationOptions(sync_mode=sync_mode)`.
  - `ReplicateHeyGenLipsync`: `video_needs_audio = True`; rewrite the docstring paragraph that claims the input video needs no audio stream — it DOES (dub-model validation: "Your video's audio is missing or corrupted"); assembly muxes the padded beat audio onto the clip before upload.

  In `app/config.py`: delete lines 62-69 (the "sync-3 generation tuning" comment block + `SYNC_OCCLUSION_DETECTION` + `SYNC_TEMPERATURE`).

- [x] **Step 4: Verify.** `python -m pytest tests/test_lipsync.py -q` → PASS. Also `grep -rn "SYNC_TEMPERATURE\|SYNC_OCCLUSION" app/ tests/` → no hits.

- [x] **Step 5: Commit.** `git add -A && git commit -m "fix(lipsync): drop dead sync-3 options; ReplicateHeyGen video_needs_audio=True"`

---

### Task 2: assembly.py — rename `replicate_lipsync` → `isolated_audio`, gate mux on `video_needs_audio`

**Files:**
- Modify: `app/assembly.py` (`assemble` signature ~730, trim-safety ~801, section-1 branches ~842-920, `_replicate_lipsync_beat` ~985-1034, dispatch ~1058-1104, drift check ~1140, `audio_baked_in` ~1168, return ~1238; docstrings)
- Test: `tests/test_assembly.py`

**Interfaces:**
- Consumes: `video_needs_audio` attribute from Task 1.
- Produces: `assemble(..., isolated_audio: bool = False, beat_audio_paths=None, beat_audio_durs=None, ...)` — the kwarg Task 3's pipeline passes. Internal `_isolated_lipsync_beat(i, clip, audio_path, clip_dur)`. Lipsync record key `output_url` (was `replicate_url`).

- [x] **Step 1: Mechanical rename in tests, then add the new failing test.** Rename all `replicate_lipsync=` kwargs in tests:

```bash
sed -i '' 's/replicate_lipsync=/isolated_audio=/g' tests/test_assembly.py tests/test_pipeline.py
```

In the two heygen-replicate assemble tests (~lines 1697 and 1807), flip the fake client to `video_needs_audio = True` (mirrors the real `ReplicateHeyGenLipsync`; keeps the `beat-0-muxed.mp4` assertions valid). Then add the new sync.so test after `test_assemble_heygen_replicate_pads_audio_to_clip_duration` (reuse that test's fixture pattern — `_linear_alignment`, `_Beat`, fakes):

```python
@pytest.mark.asyncio
async def test_assemble_syncso_isolated_uploads_silent_clip_no_mux(
    tmp_path, monkeypatch,
):
    """Sync.so accepts silent video + separate audio (no video_needs_audio
    attr) → the on_camera beat uploads the RAW silent clip and the PADDED
    isolated audio; attach_audio (the HeyGen mux) is never called for it."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)   # 1.0s VO
    beats = [_Beat(0, "X" * 10, "talking_head")]
    wins = [(0.0, 1.0)]
    clip = str(tmp_path / "clip0.mp4")
    Path(clip).touch()
    b_audio = str(tmp_path / "b0.mp3")
    Path(b_audio).write_bytes(b"")

    lipsync_calls, attach_audio_calls, pad_calls = [], [], []

    class _FakeSyncClient:  # NO video_needs_audio attribute, like SyncSoLipsync
        async def lipsync(self, *, video_url, audio_url, **kw):
            lipsync_calls.append((video_url, audio_url))

            class _Res:
                job_id, output_url = "job", "https://fake/synced.mp4"
            return _Res()

    monkeypatch.setattr(A, "_probe_duration", lambda p: 4.0)
    monkeypatch.setattr(A, "pad_audio_to",
                        lambda src, target, dest: (pad_calls.append(target),
                                                   Path(dest).write_bytes(b"")))
    monkeypatch.setattr(A, "attach_audio",
                        lambda *a, **k: (attach_audio_calls.append(a),
                                         Path(a[2]).touch()))
    monkeypatch.setattr(A, "trim_clip", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "trim_keep_audio", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "attach_silence",
                        lambda video, dest, dur, resolution=None: Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())
    monkeypatch.setattr(A, "overlay_audio",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no VO overlay in isolated mode")))

    class _FakeUp:
        def upload(self, p):
            return f"https://cdn/{Path(p).name}"

    res = await assemble(
        beats=beats, windows=wins, clip_paths=[clip], vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_FakeSyncClient(), uploader=_FakeUp(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        isolated_audio=True,
        beat_audio_paths=[b_audio], beat_audio_durs=[1.0],
    )

    assert len(lipsync_calls) == 1
    video_url, audio_url = lipsync_calls[0]
    assert video_url.endswith("clip0.mp4")          # raw silent clip, NOT *-muxed.mp4
    assert audio_url.endswith("beat-0-audio-padded.wav")
    assert attach_audio_calls == []                 # no HeyGen mux for sync.so
    assert pad_calls == [4.0]                       # padded to max(4, ceil(1.0))
    assert res.n_lipsynced == 1
```

- [x] **Step 2: Run to verify failure.** `python -m pytest tests/test_assembly.py -x -q` → FAIL (`assemble() got an unexpected keyword argument 'isolated_audio'`).

- [x] **Step 3: Implement in `app/assembly.py`.**
  - Signature: `replicate_lipsync: bool = False` → `isolated_audio: bool = False`; update every use (`replicate_lipsync` appears in the trim-safety branch ~801, section-1 routing ~842-848 and ~891, dispatch `elif replicate_lipsync:` ~1058, drift check ~1140-1142, `audio_baked_in` ~1168, `AssembledVideo.duration_s` ~1238). Update the docstring paragraph: "With ``isolated_audio=True`` (heygen_replicate engine, or syncso in per_clip granularity) each speaking beat carries its OWN isolated TTS…".
  - Rename `_replicate_lipsync_beat` → `_isolated_lipsync_beat` and make the mux conditional:

```python
    async def _isolated_lipsync_beat(
        i: int, clip: str, audio_path: str, clip_dur: float,
    ) -> tuple[int, Path]:
        """Per-beat lipsync against the beat's ISOLATED audio (heygen_replicate
        and syncso per_clip engines).

        Pads the beat's isolated audio to EXACTLY the clip duration (HeyGen
        rejects >15% audio/video mismatch; for sync.so it makes the mapping
        deterministic — sync_mode="silence" is belt-and-braces), submits the
        clip + padded audio, downloads the synced output, and trims it to the
        clip duration keeping the baked audio. No global VO overlay — the
        synced clip IS the final audio for this beat.
        """
        async with sem:
            padded = work / f"beat-{i}-audio-padded.wav"
            pad_audio_to(audio_path, clip_dur, padded)
            # HeyGen models are dub models: they validate the INPUT video has
            # an audio stream ("Your video's audio is missing or corrupted"),
            # but Seedance i2v clips are silent — mux the padded beat audio on
            # first. Sync.so takes silent video + separate audio directly.
            video_src: str | Path = clip
            if getattr(lipsync_client, "video_needs_audio", False):
                muxed = work / f"beat-{i}-muxed.mp4"
                attach_audio(clip, padded, muxed, clip_dur)
                video_src = muxed
            video_url = uploader.upload(str(video_src))
            audio_url = uploader.upload(str(padded))
            res = await lipsync_client.lipsync(
                video_url=video_url, audio_url=audio_url,
                output_file_name=f"beat-{i}",
            )
            synced = work / f"beat-{i}-synced.mp4"
            _download(res.output_url, synced)
            final = work / f"beat-{i}-final.mp4"
            trim_keep_audio(synced, clip_dur, final, resolution=clip_resolution)
            rec = {
                "index": i, "job_id": res.job_id,
                "output_url": res.output_url,
                "synced_local_path": str(synced),
                "source_local_path": clip,
                "beat_audio_path": audio_path,
                "input_video_url": video_url, "input_audio_url": audio_url,
            }
            lipsync_records.append(rec)
            if manifest is not None:
                manifest.update(lipsync=list(lipsync_records))
            if on_asset is not None:
                on_asset("lipsync_clip", rec)
            return i, final
```

  - In the resumed-beat record (~1095-1099): `"replicate_url": None` → `"output_url": None`.
  - Update inline comments that say "heygen_replicate:" on shared branches to "isolated audio (heygen_replicate / syncso per_clip):" — at ~795, ~891, ~1059, ~1315-equivalents.

- [x] **Step 4: Verify.** `python -m pytest tests/test_assembly.py -q` → PASS (heygen tests still mux via `video_needs_audio=True`; new syncso test passes). `grep -n "replicate_lipsync\|_replicate_lipsync_beat\|replicate_url" app/assembly.py` → no hits.

- [x] **Step 5: Commit.** `git add -A && git commit -m "refactor(assembly): generalize replicate_lipsync → isolated_audio; mux only for video_needs_audio clients"`

---

### Task 3: pipeline.py — `isolated_audio` predicate, threaded through prompts/TTS/clips/assemble

**Files:**
- Modify: `app/pipeline.py` (`_beat_mode` ~630, `_fallback_prompt` ~671, `_write_motion_prompts` ~737, predicate after client construction ~934, TTS gate ~1142, `_clip` ~1319/1339/1348, assemble kwargs ~1416-1450)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `assemble(..., isolated_audio=...)` from Task 2.
- Produces: `_beat_mode(beat, *, lipsync_engine, isolated_audio=False) -> str`; `_fallback_prompt(beat, window_len, *, lipsync_engine, isolated_audio=False, persona_clause="") -> str`; `_write_motion_prompts(..., isolated_audio: bool = False)`.

- [x] **Step 1: Write failing tests** in `tests/test_pipeline.py` (near the heygen_replicate block ~1520):

```python
def test_beat_mode_syncso_isolated_maps_voice_mode():
    """syncso in per_clip granularity uses isolated per-beat audio → beats
    route via resolve_voice_mode (on_camera/background_vo/silent), exactly
    like heygen_replicate — not the legacy sync_talk/broll routing."""
    from app.pipeline import _beat_mode
    b = _tbeat(shot_type="talking_head")
    assert _beat_mode(b, lipsync_engine="syncso",
                      isolated_audio=True) == "on_camera"
    bb = _tbeat(shot_type="broll", framing=None, dialogue_text="")
    assert _beat_mode(bb, lipsync_engine="syncso",
                      isolated_audio=True) == "silent"
    # legacy routing (whole granularity / direct heygen) unchanged:
    assert _beat_mode(b, lipsync_engine="syncso") == "sync_talk"


def test_fallback_prompt_syncso_isolated_uses_silent_i2v_builders():
    """Isolated syncso clips are silent i2v: on_camera → talking-motion
    prompt (mouth-moving clause for post-hoc sync), silent → b-roll prompt.
    Never the native timbre builders (those embed the dialogue line)."""
    from app.pipeline import (_fallback_prompt, _talking_motion_prompt,
                              _broll_motion_prompt)
    b = _tbeat(shot_type="talking_head")
    assert _fallback_prompt(b, 4.0, lipsync_engine="syncso",
                            isolated_audio=True) == \
        _talking_motion_prompt(b, 4.0)
    bb = _tbeat(shot_type="broll", framing=None, dialogue_text="")
    assert _fallback_prompt(bb, 4.0, lipsync_engine="syncso",
                            isolated_audio=True) == \
        _broll_motion_prompt(bb.broll_shot or "", bb.visual_note, None)
```

(Match `_tbeat`'s actual helper signature in the file — it already exists for the heygen_replicate tests.)

- [x] **Step 2: Run to verify failure.** `python -m pytest tests/test_pipeline.py -k "syncso_isolated" -q` → FAIL (`unexpected keyword argument 'isolated_audio'`).

- [x] **Step 3: Implement in `app/pipeline.py`.**
  - `_beat_mode`: add `isolated_audio: bool = False` kwarg; replace `if lipsync_engine in ("seedance_native", "heygen_replicate"):` with `if lipsync_engine == "seedance_native" or isolated_audio:`; update docstring ("isolated-audio engines (heygen_replicate always; syncso per_clip) also delegate to resolve_voice_mode…"). NOTE: `heygen_replicate` callers must now pass `isolated_audio=True` — the engine string alone no longer triggers voice-mode routing (grep for every `_beat_mode(` call).
  - `_fallback_prompt`: add `isolated_audio: bool = False` kwarg; pass it to its internal `_beat_mode` call; replace all three `if lipsync_engine == "heygen_replicate":` checks with `if isolated_audio:` (on_camera → `_talking_motion_prompt`; background_vo/silent → `_broll_motion_prompt`); update docstring.
  - `_write_motion_prompts`: add `isolated_audio: bool = False` param; pass to `_beat_mode` (~767) and `_fallback_prompt` (~768).
  - In `generate_ugc_video`, right after `lipsync_client = make_lipsync_client(...)` (~934):

```python
    # Isolated per-beat audio topology: each speaking beat gets its OWN
    # ElevenLabs TTS, that audio drives the clip duration, and the synced
    # audio stays baked in (no continuous-VO overlay → no cross-clip drift).
    # heygen_replicate always; syncso in per_clip granularity. The legacy
    # continuous-VO slicing survives only for the direct heygen engine and
    # syncso "whole".
    isolated_audio = (
        lipsync_engine == "heygen_replicate"
        or (lipsync_engine == "syncso" and lipsync_granularity == "per_clip")
    )
```

  - TTS fan-out (~1142): `if lipsync_engine == "heygen_replicate":` → `if isolated_audio:`; update the comment block above it (drop "heygen_replicate only").
  - `_write_motion_prompts` call (~1288): add `isolated_audio=isolated_audio`.
  - `_clip` (~1319): duration gate `if lipsync_engine == "heygen_replicate":` → `if isolated_audio:`; `_beat_mode` call (~1339) gains `isolated_audio=isolated_audio`; silent-i2v branch (~1348) `elif lipsync_engine == "heygen_replicate":` → `elif isolated_audio:` (comment: "isolated-audio engines: always silent i2v; per-beat audio applied in assembly").
  - Assemble block (~1416-1450):

```python
    if lipsync_engine == "seedance_native":
        _assemble_desc = "assembling (speech-end trim → per-clip audio → concat)..."
    elif isolated_audio:
        _assemble_desc = (
            "assembling (pad beat audio → per-beat lipsync "
            f"[{lipsync_engine}] → concat, audio baked in)..."
        )
    elif whole_video_lipsync:
        _assemble_desc = "assembling (trim → concat → ONE whole-video lipsync)..."
    else:
        _assemble_desc = "assembling (trim → parallel lipsync → concat → overlay VO)..."
```

  and in the `assemble(...)` call: `isolated_audio=isolated_audio`, `beat_audio_paths=(beat_audio_paths if isolated_audio else None)`, `beat_audio_durs=(beat_audio_durs if isolated_audio else None)`, `lipsync_concurrency=(REPLICATE_MAX_CONCURRENCY if lipsync_engine == "heygen_replicate" else SYNC_MAX_CONCURRENCY)` (unchanged — the cap is per PROVIDER, not per topology).
  - Comment at ~883 ("HeyGen needs per-clip audio muxing…") — reword for the shared topology.

- [x] **Step 4: Run the full suite; fix mechanical stragglers.** `python -m pytest tests/ -q`. Any remaining failures should only be: heygen_replicate tests calling `_beat_mode`/`_fallback_prompt` relying on engine-string routing (add `isolated_audio=True`), or assemble-kwarg assertions (already sed-renamed in Task 2). Fix and re-run to green.

- [x] **Step 5: Commit.** `git add -A && git commit -m "feat(pipeline): syncso per_clip switches to isolated per-beat audio"`

---

### Task 4: Config/docs coherence + final verification

**Files:**
- Modify: `app/config.py:71-83` (`SYNC_LIPSYNC_GRANULARITY` comment)
- Modify: `tasks/todo.md` (review section), `tasks/lessons.md` if anything was learned

- [x] **Step 1: Rewrite the granularity comment** in `app/config.py`:

```python
# Sync.so lipsync granularity (syncso engine only):
#   "per_clip" (default) → ISOLATED per-beat audio (same topology as
#       heygen_replicate): each speaking beat gets its own ElevenLabs TTS,
#       the audio drives the clip duration (max(4, min(15, ceil(audio)))),
#       audio is padded to exactly the clip length, each beat is lipsynced
#       against ITS OWN audio, and the synced audio stays baked in — no
#       continuous-VO overlay, no cross-clip A/V drift. Only speaking beats
#       are billed; b-roll/reaction beats never touch Sync.so.
#   "whole" → concat ALL beats into the silent final cut FIRST, then send that
#       single video + the FULL continuous VO to Sync.so in ONE pass (the synced
#       output already carries the VO). EXPERIMENTAL: sync-3 has no "skip this
#       segment" flag, so b-roll/reaction beats are also driven by the audio
#       AND you pay per second for their footage too. A/B comparison only.
```

- [x] **Step 2: Full verification.**
  - `python -m pytest tests/ -q` → all green.
  - `grep -rn "replicate_lipsync\|SYNC_TEMPERATURE\|SYNC_OCCLUSION" app/ tests/` → no hits.
  - `grep -rn 'lipsync_engine == "heygen_replicate"' app/pipeline.py` → only the concurrency pick (and nothing on the topology path).

- [x] **Step 3: Update `tasks/todo.md`** review section (what changed, verification output) and commit: `git add -A && git commit -m "docs(config): document syncso per_clip isolated-audio semantics"`

---

## Self-review

- Spec coverage: predicate ✔ (T3), voice-mode routing ✔ (T3), prompts ✔ (T3), TTS fan-out ✔ (T3), clip ordering ✔ (T3), assemble flags/concurrency ✔ (T3), rename + conditional mux + neutral record key ✔ (T2), pad/trim mismatch handling ✔ (unchanged code, asserted in T2 test), sync-3 option cleanup ✔ (T1), whole/heygen/native untouched ✔ (T3 predicate + tests), docs ✔ (T4).
- Type consistency: `isolated_audio: bool` everywhere; `_isolated_lipsync_beat(i, clip, audio_path, clip_dur)` matches dispatch call.
