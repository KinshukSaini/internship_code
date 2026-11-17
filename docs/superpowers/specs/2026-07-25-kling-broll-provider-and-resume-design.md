# Kling v3 b-roll provider + resume a partially-paid run — Design

**Date:** 2026-07-25
**Status:** Approved design, pre-implementation
**Scope:** Two independent cost features on the v2 pipeline — (1) route **b-roll only** to
`kling-v3-image-to-video` on the same EvoLink account (~2.5× cheaper per second), UI-selectable and
**default OFF**; (2) **resume** a run that already paid for clips instead of re-ordering them.

---

## 1. Motivation — the spend numbers

EvoLink billing history (via the undocumented `/v1/credits` + `/v1/tasks` endpoints, see
`docs/` memory note *EvoLink billing & usage API*; 1 credit = **$0.014722**):

| Metric | Value |
|---|---|
| Credits consumed | **72,051 cr = $1,060.42** |
| Credits remaining | **~$100** |
| Runs billed | **119** |
| Median cost per run | **$8.91** |
| Seconds ordered above the beat window (4s API floor) | **25.5 %** — paid, then trimmed away |
| Paid clips stranded in abandoned runs | **$25.64** across **5** run dirs |

Two structural leaks fall out of that table, and this design attacks one each:

1. **Every second of b-roll is billed at talking-head prices.** Seedance 2.0 is 13.5 cr/s
   ($0.199/s). A typical 10-beat ad is ~60 % b-roll/reaction/silent seconds — none of which need
   Seedance's audio-driven or lipsync-friendly behaviour. Kling v3 i2v is 5.4 cr/s ($0.0795/s) on
   the *same* endpoint and the *same* API key.
2. **A run that dies after the clip stage throws away everything it paid for.** Resume exists
   nowhere: it is dead code (see §7), so the operator's only recovery is a full re-run at full
   price. `jobs/workflows/v2-20260702-153827` has **10/10 beats already on disk** — assembling it
   costs **$0** and today there is no button that does it.

Trim waste (25.5 %) is deliberately *not* in scope here — it is a duration/planner problem, and
mixing it in would muddy the provider A/B.

---

## 2. Decisions (locked)

| # | Decision | Choice | Why |
|---|---|---|---|
| D1 | Kling rollout | `BROLL_VIDEO_PROVIDER = "seedance"` — **default OFF** | Today's output is the approved quality bar; a cheaper provider must earn its way in via an operator-chosen A/B, not by silently changing every run. |
| D2 | Which beats | **b-roll / reaction / silent / background_vo only**, never talking heads | Kling has no audio-driven generation and no reference-to-video; talking beats depend on either audio_urls (native) or a mouth-moving silent i2v that we have already tuned for sync-3/HeyGen. |
| D3 | Resolution | Kling **follows the run's `clip_resolution`** (no separate control) | Mixed-resolution clips would force a rescale in `finish()` and break shot matching. `480p` clamps up to Kling's `720p` floor (still cheaper/s than Seedance 480p). |
| D4 | Client shape | **One `EvoLinkVideoClient`, body-builder dispatch** on the model family | Same host, same Bearer auth, same `/v1/tasks/{id}` polling, same task-id semantics — only the request body differs. A second client class would fork the hardened poll/retry/deadline code from commit `914536f`, which is exactly the thing that must not be forked. |
| D5 | Kling prompts | **New sibling builder** `_kling_broll_prompt` — Opus writes the motion, code repairs it | Kling's prompt contract is *fundamentally* different from Seedance's (§4). Reusing `_broll_motion_prompt` would ship Kling's documented worst failure mode. Repair is deterministic in code, never by re-prompting (project rule, `tasks/lessons.md`). |
| D6 | Resume ordering | **local clip → re-download → re-order**, in that order | 3 of the 5 stranded runs predate task-id persistence (`clip_task_ids: {}`) but *do* have valid clips on disk. A task-id-first design would have recovered $0 of them. |
| D7 | Provider trust | A live task record is **not** proof the artifact is fetchable — range-GET before trusting | Verified: `/v1/tasks/{id}` keeps reporting `completed` with a `result_data` URL long after the file itself 404s (retention ~72 h). |
| D8 | Cost visibility | Log a per-run estimate **broken down by provider** | The account is at ~$100. The operator must be able to see what a run will cost and what a resume saved. |

---

## 3. Verified provider facts (Kling v3 on EvoLink)

Sources: EvoLink model schema for `kling-v3-image-to-video`, Kling's official i2v prompt guidance,
fal's Kling prompt-length guidance, and our own `/v1/credits` + `/v1/tasks` billing history.

**Transport — identical to Seedance.**
`POST /v1/videos/generations` with `Authorization: Bearer <EVOLINK_API_KEY>`, returns a task id,
polled with `GET /v1/tasks/{id}`. Same status vocabulary, same `results[]` shape. **Only the request
body and the prompt shape differ.**

**Pricing.** `kling-v3-image-to-video` = **5.4 cr/s ($0.0795/s)** at 720p vs `seedance-2.0` =
**13.5 cr/s ($0.199/s)**. ≈2.5× cheaper. Confirmed independently from the EvoLink docs *and* from
our billing history.

**Body fields.**

| Field | Type | Notes |
|---|---|---|
| `model` | string, **required** | `kling-v3-image-to-video` |
| `image_start` | uri, **required** | the anchor frame (Seedance's `image_urls[0]`) |
| `image_end` | uri, optional | last-frame; we don't use it today but the field exists |
| `prompt` | string, optional | ≤ 2500 chars |
| `negative_prompt` | string, optional | ≤ 2500 chars — **Kling has this, Seedance does not** |
| `duration` | **integer**, default 5 | range **3–15** (our `_requested_duration` floor of 4 stays valid) |
| `quality` | string, default `"720p"` | `"720p"` / `"1080p"` / `"4k"` — **no 480p** |
| `sound` | **string** `"on"`/`"off"`, default `"off"` | *not* a boolean — a `False` here is a schema error |
| `model_params` | object, optional | unused |
| `callback_url` | string, optional | unused (we poll) |

**Fields Kling i2v does NOT have:** `aspect_ratio` (derived from `image_start`), `seed`
(generation is non-reproducible), `camera_fixed`, `cfg_scale` (absent on EvoLink),
`camera_control` (v1-family only), and no audio input of any kind.

**`kling-v3-turbo-image-to-video` is rejected**: no `negative_prompt`, no `image_end`, no `sound`,
and it costs *more*. Every lever this design leans on is missing from turbo.

**File retention ≈ 72 h.** Probed by age against the result CDN: 0/1/2-day-old artifacts answer a
range request with HTTP 206; 4-day-and-older return 404. This is the fact behind D7 and behind
resume tier 2's mandatory range-GET.

---

## 4. Why Kling prompts cannot reuse `_broll_motion_prompt`

Kling's official i2v formula is **"Subject + Movement, Background + Movement"** — *motion only*.
Its documented failure mode is the exact thing our Seedance builder does on purpose:

> "a description that significantly deviates from the image may cause a camera cut or transition"

`_broll_motion_prompt` (`app/pipeline.py:635`) emits ~180–250 words that **re-describe the
product** — shape, label, printed text, material — because on Seedance that redundancy is what
defends the anchor's realism through frames 2..N. On Kling that same re-description *is* the
deviation, and the model answers it with a cut or a scene change mid-clip.

Four concrete translations:

1. **Bans move to `negative_prompt`.** The comment at `app/pipeline.py:471` explains that our bans
   are phrased *affirmatively* only because "Seedance has NO negative_prompt field, and 'no plastic
   skin' can BACKFIRE by surfacing the very noun it negates." Kling *has* the field, so the bans
   move there as bare comma-separated noun phrases — **never** `"no X"` phrasing.
2. **Camera lock has no flag.** There is no `camera_fixed`. Locking the camera on Kling = **omit
   all camera language from the positive prompt** and put the camera terms (`zoom`, `pan`,
   `camera shake`) in the negative.
3. **`_SETTLE_CLAUSE` is dangerous here.** Kling warns that vague prompts can return a **frozen /
   static** clip. Telling it to "settle into a calm, steady, held framing for the final second" on
   a reaction beat is an invitation to hand back a dead clip. The Kling reaction path uses a
   **named micro-motion** instead (a blink, a breath, a small head tilt), and `frozen` / `static`
   go in the negative.
4. **Length inverts.** Kling docs say "use simple words and sentence structures"; fal's guidance is
   50–150 words. Target **≤ 400 chars** for the positive prompt (hard cap 2500).

---

## 5. Feature 1 — Kling as b-roll video provider

### 5.1 `app/config.py`

Follows the existing `DEFAULT_X` + `XS`-tuple convention (`CLIP_RESOLUTIONS`, `FINISHING_LOOKS`,
`TRANSITION_STYLES`):

```python
BROLL_VIDEO_PROVIDER = "seedance"                 # default = today's behaviour, unchanged
BROLL_VIDEO_PROVIDERS = ("seedance", "kling")
KLING_BROLL_MODEL = "kling-v3-image-to-video"
KLING_QUALITIES = ("720p", "1080p", "4k")
```

### 5.2 `app/video_gen.py` — one client, two body builders

- **Rename** `EvoLinkSeedanceGenerator` → **`EvoLinkVideoClient`** (the class is no longer
  Seedance-specific), and keep `EvoLinkSeedanceGenerator = EvoLinkVideoClient` as a
  backwards-compat alias so `app/chain.py` and the existing tests keep importing the old name.
  Update `make_video_client`'s return annotation.
- **Do not touch** `create_task`, `poll_task`, `poll_task_stream`, `create_task_async`,
  `poll_task_stream_async`, `close`, `_headers`, `_extract_video_url`. That poll/retry/deadline code
  is the hardened fix from commit `914536f` (sync download inside an async fan-out wedged a whole
  run — `tasks/lessons.md`, "Sync network calls inside async fan-outs wedge the whole run"). It is
  provider-agnostic already; forking or refactoring it is out of bounds.
- **`VideoRequest`** gains exactly one field: `negative_prompt: str | None = None`.
- **`provider_for_model(model: str) -> str`** (module-level): `"kling"` if the id starts with
  `kling`, `"seedance"` if it starts with `seedance`, else **raise `ValueError`** — an unknown model
  family must fail before a paid call, not silently get a Seedance body.
- **`_build_body` becomes a dispatch**:

  ```python
  def _build_body(self, request):
      return _BODY_BUILDERS[provider_for_model(request.model)](request)
  ```

  - `_seedance_body(request)` — **exactly** the current `_build_body` logic, byte-for-byte
    identical behaviour (`image_urls`, `aspect_ratio`, `generate_audio`, conditional
    `camera_fixed` / `audio_urls` / `seed`).
  - `_kling_body(request)`:
    - **raise `ValueError` if `request.audio_url` is set** — Kling has no audio-driven generation,
      so reaching here with audio is a routing bug in `_use_kling`, and it must surface as a
      programming error rather than a silently-mute clip we paid for.
    - emits `{"model", "image_start": request.first_frame_url, "prompt", "duration",
      "quality": _kling_quality(request.resolution), "sound": "off"}`
    - `"image_end"` only when `request.last_frame_url` is set
    - `"negative_prompt"` only when `request.negative_prompt` is set
    - **never** emits `seed` / `camera_fixed` / `aspect_ratio` / `generate_audio` / `image_urls` —
      Kling's schema has none of them and unknown keys are a 400 on a fully-billable call.
- **`_kling_quality(res)`**: `"480p"` → `"720p"` (clamp **up** to Kling's floor — there is no 480p,
  and 720p Kling is still cheaper per second than 480p Seedance); pass through when in
  `KLING_QUALITIES`; else `ValueError`.

### 5.3 `app/pipeline.py` — the routing predicate (the correctness crux)

```python
def _use_kling(mode: str, *, native: bool, broll_provider: str) -> bool:
    """Kling handles beats that are neither lip-synced nor audio-driven."""
    if broll_provider != "kling" or native:
        return False
    return mode in ("broll", "reaction", "silent", "background_vo")
```

`mode` is the value already produced by `_beat_mode` (`app/pipeline.py:687`).

**Why the `native` guard is essential.** Under `lipsync_engine="seedance_native"`, a
`background_vo` beat is *visually* b-roll but is generated **with audio** on the
reference-to-video model: `_clip_request` sets `model=NATIVE_LIPSYNC_MODEL`,
`generate_audio=True`, and `audio_url=<the shared timbre anchor>` (`app/pipeline.py:909`). Kling
cannot do audio-driven generation at all. Without the `native` short-circuit, the cheapest-looking
beats in native mode would route to a provider that physically cannot produce their audio — and
`_kling_body`'s `audio_url` guard would then abort the run mid-clip-stage, after the earlier beats
were already billed. So: **in native mode, nothing goes to Kling.**

`sync_talk`, `on_camera` and `native_talk` **always** stay on Seedance (D2).

### 5.4 `app/pipeline.py` — `_kling_broll_prompt` (a sibling, not a tweak)

```python
def _kling_broll_prompt(beat, ...) -> tuple[str, str]:   # (positive, negative)
```

**Positive** — at most three short sentences, target ≤ 400 chars:

1. one **motion** sentence (subject + movement, background + movement),
2. optionally one **camera** sentence — *omitted entirely* when the shot should be locked,
3. the tail `"Preserve the composition and colors of the reference image."`

The motion comes from **Opus, through the same LLM seam as the existing b-roll prompt** (stage 5b,
`_write_motion_prompts`, `app/pipeline.py:802`) with a **Kling-specific instruction**. Then a
**deterministic validator/repair runs in code** — computable failures are fixed in code, never by
re-prompting (project rule):

- hard-truncate at the char cap **on a sentence boundary** (never mid-clause),
- **strip camera-movement words** when the shot should be locked (there is no `camera_fixed`),
- **guarantee the "Preserve the composition and colors" tail is present exactly once** (Opus may
  paraphrase it away — the same failure mode that forced the anatomy/product-stability
  force-append, see the 2026-07-22 review in `tasks/todo.md`).

**Negative constants** — two sets, picked by what is in frame:

- `_KLING_NEG_PRODUCT` — for product/hands b-roll (`_PRODUCT_HANDS_BROLL_TYPES` /
  `_HANDS_BROLL_TYPES`, `app/pipeline.py:251,269`):
  `morphing product, warping label, distorted text, floating text, peeling label, product changing
  size, product changing shape, duplicate product, new object entering frame, cap opening,
  spraying, extra hand, disembodied hand, extra fingers, warped hands, scene cut, transition,
  camera shake, zoom, pan, blur, distortion, low quality, text overlay, watermark, cartoon`
- `_KLING_NEG_PERSON` — for person-in-frame shots (lifestyle b-roll **and** reaction cutaways):
  `distorted face, changing face, extra fingers, warped hands, extra limbs, plastic skin, waxy
  skin, oily skin, blotchy skin, scene cut, transition, camera shake, blur, distortion, low
  quality, text overlay, watermark, cartoon, slow motion, frozen, static`

Each list is the negative-prompt translation of a ban we already ship affirmatively for Seedance:
`cap opening` / `spraying` is the never-spray-a-sealed-bottle rule; `product changing size/shape`
and `peeling label` are `_PRODUCT_STABILITY_CLAUSE`; `frozen, static` is the Kling-specific
anti-dead-clip guard from §4.3.

**Skin-texture negatives stay flattering** (existing project rule): ban *plastic / waxy / oily /
blotchy*. Never ask for pores, oil or peach-fuzz emphasis — that makes a good-looking creator read
blotchy, which is exactly the regression `_SKIN_TEXTURE_CLAUSE`'s comment (`app/pipeline.py:471`)
was written to prevent.

**Interception point.** Prompts are built in stage 5b, an **earlier stage than the clip fan-out**,
so `broll_video_provider` must thread into **both**. `_fallback_prompt` (`app/pipeline.py:732`)
funnels `background_vo`, `silent` **and** `reaction` into `_broll_motion_prompt` — that is the one
clean place to branch. `manifest["motion_prompts"]` entries gain `"provider"` and
`"negative_prompt"` so a bad clip can be diagnosed from the manifest alone.

### 5.5 `app/pipeline.py` — threading, request build, cost log

- `generate_ugc_video` gains `broll_video_provider: str = BROLL_VIDEO_PROVIDER`, validated against
  `BROLL_VIDEO_PROVIDERS` **before any client is built** — the same fail-fast block that already
  guards `_LIPSYNC_ENGINES` / `_LLM_PROVIDERS` / `TRANSITION_STYLES` (`app/pipeline.py:967`).
- `_clip_request` gains the Kling branch: `model=KLING_BROLL_MODEL`,
  `negative_prompt=<the negative>`. `camera_fixed`, `seed` and `generate_audio` become irrelevant —
  `_kling_body` drops them, so the call site does not need to special-case them.
  `_requested_duration` is **unchanged**: Kling's range is 3–15, so the existing floor of 4 and cap
  are already legal.
- Each `manifest["clips"][i]` entry records the **provider actually used**.
- A **per-run cost estimate broken down by provider** is logged via `on_progress`:
  seconds × 13.5 cr/s for Seedance beats, × 5.4 cr/s for Kling beats, × $0.014722/cr, plus the
  total and the delta vs all-Seedance. D8.

### 5.6 `app/ui.py`

A `gr.Radio` **"B-roll video provider"** next to `clip_resolution` (`app/ui.py:1395`):

```python
choices=[("Seedance (all clips)", "seedance"),
         ("Kling v3 (b-roll only — ~2.5x cheaper)", "kling")],
value=BROLL_VIDEO_PROVIDER,
```

Threaded exactly like `clip_resolution`: component → `gen_btn.click(inputs=[...])`
(`app/ui.py:1688`) → `on_generate_v2` positional parameter → `generate_ugc_video` kwarg
(`app/ui.py:1586`). **Adding to the `inputs` list shifts every later positional parameter** — the
handler signature and the `inputs=[...]` order must be edited together and stay in lockstep.
(Every new pipeline knob reaches the UI with a config default — project rule,
`tasks/lessons.md:61`.)

---

## 6. Feature 2 — resume a partially-paid run

### 6.1 Verified state of the stranded runs

`ffprobe`-verified, all 5 have their paid clips on disk as complete `clip-N.mp4`:

| Run | clips on disk | beats | `clip_task_ids` |
|---|---|---|---|
| `v2-20260702-153827` | **10** | 10 | `{}` — **complete, assembling costs $0** |
| `v2-20260709-191535` | 4 | 10 | `{}` |
| `v2-20260709-195555` | 1 | 10 | `{}` |
| `v2-20260714-131011` | 10 | 11 | 10 |
| `v2-20260714-154239` | 5 | 11 | 5 |

The three pre-`914536f` runs have **no task ids at all** but do have clips. That single fact
dictates D6: resume must be **local-clip-first**; re-download is a best-effort second tier;
re-ordering is last.

### 6.2 The two independent bugs (both `app/pipeline.py`)

1. **`_Manifest` has no `get` method.** The class (`app/pipeline.py:153`) defines only
   `__init__`, `update`, `add_step`, `mark`, `path`, `_save`. So this line:

   ```python
   prior_lipsync_records=manifest.get("lipsync") if hasattr(manifest, "get") else None,
   ```

   (`app/pipeline.py:1637`) is **permanently `None`** — the `hasattr` guard can never be true. Every
   talking head is therefore re-lipsynced on every run, at full Sync.so/Replicate price, even when
   a perfectly good lipsynced clip is already recorded in the manifest.
2. **`clip_task_ids` is write-only.** It is persisted at `app/pipeline.py:1548-1552` (correctly, at
   task *acceptance* — the `914536f` fix) and read back by **nothing**. The only manifest reader is
   the one-off `scripts/resume_assemble.py`, and it reads `m["clips"]` / `m["lipsync"]`, not task
   ids. Compounding it, `_Manifest.__init__` **always** builds a fresh `_data` dict — it never
   rehydrates from an existing file, so even a correct reader would see an empty manifest.

### 6.3 Fixes

- **`_Manifest.get(key, default=None)`** → `self._data.get(key, default)`.
- **`_Manifest.load(path)`** classmethod: rehydrate `_data` from an existing `manifest.json`,
  preserving the `version` field; raise a clear error when the file is missing or unparseable
  (a silent empty rehydrate would make resume re-order every paid clip).
- **`app/pipeline.py:1637`** → plain `manifest.get("lipsync")`; delete the dead `hasattr` guard.
- **`resumable_runs(jobs_root) -> list[...]`**: scan `*/manifest.json` for runs lacking
  `final.mp4`; per run report the run id, beat count, which beat indices have a **valid local
  clip**, which have a **paid task id**, and which are **missing entirely**.
- **`resume_ugc_video(run_dir, *, settings, ...)`**: rehydrate from the manifest — it already
  persists `timeline.beats`, `beat_windows`, `anchors`, `motion_prompts`, `vo_audio_path`,
  `beat_audio_paths`, `beat_audio_durs`, `beat_alignments`, `character_ref_url`,
  `product_ref_normalized`, `voice` and the run params — then resolve each beat's clip in **tiers**.

### 6.4 The tier model

| Tier | Condition | Action | Cost |
|---|---|---|---|
| **1** | `clip-N.mp4` exists **and** `ffprobe` reports duration > 0 | reuse in place | **free** |
| **2** | `clip_task_ids[N]` present → `GET /v1/tasks/{id}` says `completed` **and** a **range-GET of the result URL returns 2xx** | download | **free** |
| **3** | otherwise | re-order through the normal clip path | **paid** |

Then continue into lipsync (now correctly reusing `prior_lipsync_records`) and assembly as usual.
The **tier chosen per beat** and the **total paid cost** are logged.

**Why local-first (D6).** Task ids only exist for runs after `914536f`; local clips exist for all
5 stranded runs. Local-first recovers 30/30 stranded clips; task-id-first recovers 15/30 and
re-pays for the rest.

**Why the range-GET is mandatory in tier 2 (D7).** EvoLink keeps reporting a task as `completed`
with a `result_data` URL long after the artifact itself 404s (retention ~72 h, §3). Trusting the
task record alone would send resume down a "free" path that hands back a 404 mid-download, after
it has already skipped the paid re-order.

**Idempotence.** Resume must be safe to call twice: tier 1 makes a second invocation a no-op for
every beat the first one landed, and the manifest is updated through the same `_Manifest` atomic
write so a crash mid-resume leaves a consistent file.

### 6.5 `app/ui.py` resume surface

- A **dropdown of resumable runs**, labelled e.g. `v2-20260714-131011  (10/11 clips paid)`.
- A **"Preview resume"** button that shows the per-beat tier breakdown **and the estimated cost
  before anything is committed** — no paid call happens until the operator sees the number.
- A **"Resume run"** button that executes it.
- Refreshing the dropdown must **not** require an app restart (re-scan on click, the way the
  existing refresh controls do it).

---

## 7. Non-goals

- **No Kling for talking heads** — `sync_talk` / `on_camera` / `native_talk` stay on Seedance
  (D2). Kling has no audio-driven or reference-to-video mode.
- **No `kling-v3-turbo-image-to-video`** — it drops `negative_prompt`, `image_end` and `sound`,
  and costs more (§3).
- **No 4k**: `KLING_QUALITIES` lists it because the schema does, but the UI exposes only the run's
  own `clip_resolution` (D3) and assembly's `_RES_DIMS` has no 4k entry.
- **No per-beat provider override** — one provider choice per run. Per-beat would multiply the
  A/B matrix before we have a single side-by-side result.
- **No `image_end` / last-frame usage on Kling** — the field is documented for completeness; the
  pipeline passes `last_frame_url=None` today and that does not change.
- **No 4s-floor trim-waste fix** (the other 25.5 %) — separate change, deliberately not mixed in.
- **No `seed` reproducibility on the Kling path** — the model has no seed; runs are
  non-reproducible there by construction, and `_seed_for` keeps serving the Seedance beats.
- **No new provider host / SDK** — Kling rides the existing EvoLink key and transport (D4).
- **Resume does not re-plan** — it never re-runs the script/timeline/anchor stages, and it never
  re-prompts. A manifest whose earlier stages are incomplete is not resumable, it is a fresh run.

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Kling b-roll reads visibly cheaper than Seedance b-roll | Default OFF (D1); operator A/Bs a real run before adopting; per-clip provider is in the manifest so the comparison is auditable. |
| Kling returns a frozen/static clip on a reaction beat | Drop `_SETTLE_CLAUSE`, use a named micro-motion, put `frozen, static` in the negative (§4.3). |
| Kling cuts/transitions mid-clip | Positive prompt is motion-only, ≤400 chars, no product re-description; `scene cut, transition` in the negative (§4.1). |
| Opus paraphrases away the preserve-composition tail | Deterministic repair guarantees it exactly once (§5.4) — same force-append pattern as the anatomy/product clauses. |
| Native mode silently routes audio beats to Kling | `_use_kling`'s `native` guard **plus** `_kling_body`'s `audio_url` `ValueError` as a second, load-bearing net (§5.3). |
| Unknown-key 400 on a billable Kling call | `_kling_body` emits an explicit allow-list; it never forwards Seedance-only keys. |
| Resume "free" tier hands back a 404 | Mandatory range-GET before trusting a `completed` task (D7). |
| Resume double-charges on a second invocation | Tier 1 short-circuits every already-landed beat; resume is idempotent (§6.4). |
| UI positional-parameter drift when the new radio is added | `inputs=[...]` order and the handler signature edited together; a test asserts the kwarg reaches `generate_ugc_video`. |

---

## 9. Files touched

- `app/config.py` — `BROLL_VIDEO_PROVIDER`, `BROLL_VIDEO_PROVIDERS`, `KLING_BROLL_MODEL`,
  `KLING_QUALITIES`.
- `app/video_gen.py` — rename to `EvoLinkVideoClient` + compat alias; `VideoRequest.negative_prompt`;
  `provider_for_model`; `_build_body` dispatch → `_seedance_body` / `_kling_body`; `_kling_quality`.
  Poll/retry/deadline code untouched.
- `app/pipeline.py` — `_use_kling`; `_kling_broll_prompt` + `_KLING_NEG_PRODUCT` /
  `_KLING_NEG_PERSON` + the deterministic repair; `broll_video_provider` threaded through
  `generate_ugc_video` → `_write_motion_prompts` / `_fallback_prompt` → `_clip_request`; per-clip
  provider + per-run cost estimate in the manifest/log; `_Manifest.get` + `_Manifest.load`;
  `:1637` lipsync-reuse fix; `resumable_runs`; `resume_ugc_video`.
- `app/ui.py` — "B-roll video provider" radio (threaded like `clip_resolution`); resume dropdown +
  preview + "Resume run", with restart-free refresh.
- `tests/` — new coverage per §10.

`scripts/resume_assemble.py` stays as-is: it is the pre-existing one-off, and `resume_ugc_video` is
the supported replacement rather than a rewrite of it.

---

## 10. Verification

**Unit (mocked — no live provider call at any point).**

- `provider_for_model`: `kling*` → `"kling"`, `seedance*` → `"seedance"`, anything else raises.
- `_seedance_body` is byte-identical to today's `_build_body` across the existing matrix
  (i2v silent, native r2v with `audio_urls`, `camera_fixed` on/off, seed present/absent).
- `_kling_body`: required keys present; `sound` is the **string** `"off"`; `duration` is an int;
  `image_end` / `negative_prompt` appear only when set; `seed` / `camera_fixed` / `aspect_ratio` /
  `generate_audio` / `image_urls` **never** appear; `audio_url` set → `ValueError`.
- `_kling_quality`: `480p`→`720p`, `720p`/`1080p`/`4k` pass through, anything else raises.
- `_use_kling`: truth table over `mode` × `native` × `broll_provider`, with an explicit case
  asserting `background_vo` + `native=True` stays on Seedance.
- `_kling_broll_prompt`: ≤400 chars; truncation lands on a sentence boundary; camera words stripped
  on locked shots; preserve-composition tail present **exactly once** even when Opus omits or
  duplicates it; product vs person negative chosen by `broll_type`; no `"no X"` phrasing in the
  negative; skin negatives contain `plastic`/`waxy` and contain **no** pore/oil-emphasis term.
- `EvoLinkSeedanceGenerator is EvoLinkVideoClient` (alias holds).
- `_Manifest.get` returns stored values and the default; `_Manifest.load` round-trips a written
  manifest and raises on missing/corrupt input.
- `prior_lipsync_records` is actually populated from the manifest (the `:1637` regression test).
- `resumable_runs` on a fixture `jobs/` tree: classifies beats into local / task-id / missing and
  excludes runs that already have `final.mp4`.
- `resume_ugc_video` tier selection: tier 1 on a valid local clip; tier 2 only when the task says
  `completed` **and** the stubbed range-GET returns 2xx; tier 2 → tier 3 when the range-GET 404s;
  tier 3 re-orders; a second invocation re-orders **nothing** (idempotence); the cost log reports
  only tier-3 seconds.
- UI: the radio's value reaches `generate_ugc_video(broll_video_provider=...)` and the positional
  order of `inputs=[...]` matches the handler signature.

**Suite gate.** `python -m pytest tests/ -x -q` green (424 at the time of writing).

**Live validation is CREDIT-GATED** (~$100 left, ABSOLUTE RULE: no live call without an explicit
top-up + go-ahead). When unblocked, in this order:
1. Assemble `v2-20260702-153827` via resume — **must cost $0** (10/10 tier 1). The cheapest
   possible proof that resume works.
2. Resume `v2-20260714-131011` (10/11) — 10 beats free, 1 paid; confirms tiers 1+3 and, if the
   artifact is younger than 72 h, tier 2.
3. One short all-Kling-b-roll run at 720p — check for mid-clip cuts, frozen reaction beats, product
   label/size drift, skin plasticity; A/B the same brief against all-Seedance and compare both the
   look and the logged cost delta.

---

## 11. Expected saving

For a 10-beat run at ~5s/clip with ~6 b-roll/reaction/silent beats:

- All-Seedance: 50s × 13.5 cr/s = 675 cr ≈ **$9.94** (matches the $8.91 median).
- Kling b-roll: (20s × 13.5) + (30s × 5.4) = 270 + 162 = 432 cr ≈ **$6.36** — **~36 % less**.

Plus resume: the $25.64 already stranded becomes recoverable, and the first recovery
(`v2-20260702-153827`) is free.
