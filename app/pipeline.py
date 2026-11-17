"""Headless end-to-end UGC video pipeline (Phase 4).

Ties the pieces together:
  1. plan_timeline (Gemini)          → script + two-track beats
  2. generate_vo (ElevenLabs)        → audio + char timestamps  [AUDIO FIRST]
  3. trim_silences                   → dead air cut from VO + alignment
  4. beat_windows                    → [start,end] per beat
  5. character ref + per-beat anchors (Nano Banana, identity/product locked)
  6. single-image clips (Seedance i2v: silent; or audio-driven r2v against
     the beat's VO slice when lipsync_engine="seedance_native")
  7. assemble (trim → parallel Sync.so lipsync → concat → overlay VO; native
     mode keeps each clip's own audio and the concat IS the final)

The UI is a thin caller of generate_ugc_video(); this module is the engine
and is independently scriptable for smoke tests.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from app import costs
from app.assembly import (
    _probe_duration,
    _strip_tags,
    apply_holds,
    assemble,
    beat_windows,
    merge_short_windows,
    pad_audio_to,
    slice_audio,
    split_long_windows,
)
from app.config import (
    BROLL_VIDEO_PROVIDER,
    BROLL_VIDEO_PROVIDERS,
    BURN_CAPTIONS,
    CAPTION_WORDS_PER_LINE,
    CLAUDE_PRO_MODEL,
    COLOR_MATCH_CLIPS,
    DEFAULT_CLIP_RESOLUTION,
    DEFAULT_LIPSYNC_ENGINE,
    CARTESIA_MAX_CONCURRENCY,
    ELEVENLABS_MAX_CONCURRENCY,
    FISH_MAX_CONCURRENCY,
    FINISHING_GRADE,
    FINISHING_LOOK,
    FINISHING_LOOKS,
    KLING_BROLL_MODEL,
    LOUDNORM_I,
    OMNI_DUR_MAX,
    OMNI_DUR_MIN,
    OMNI_MULTISHOT,
    OMNI_SHOT_MAX_S,
    OMNI_SHOT_TARGET_S,
    OMNI_SPEECH_HEADROOM_S,
    OMNI_EVOLINK_FALLBACK,
    OMNI_EVOLINK_MODEL,
    OMNI_EVOLINK_USD_PER_SECOND,
    OMNI_USD_PER_SECOND,
    OMNI_VIDEO_MODEL,
    OMNI_WORDS_PER_SECOND,
    VIDEO_GENERATOR,
    VIDEO_GENERATORS,
    VISUAL_QC,
    NATIVE_LIPSYNC_MODEL,
    PUNCH_IN_ACCENT,
    ROOM_TONE,
    SYNC_GRANULARITIES,
    SYNC_LIPSYNC_GRANULARITY,
    REPLICATE_MAX_CONCURRENCY,
    SYNC_MAX_CONCURRENCY,
    USE_CLONED_VOICE_LIBRARY,
    USE_PRONUNCIATION_FIX,
    TRANSITION_STYLE,
    TRANSITION_STYLES,
    VOICE_PROVIDER,
    VOICE_PROVIDERS,
    Settings,
)
from app.gemini import (
    GeminiClient, TimelineBeat, render_voice_persona, _DEXTERITY_RE,
    _FRAMING_MOTION_CAMERA,
)
from app.image_gen import make_image_client
from app.lipsync import make_lipsync_client
from app.pacing import trim_silences
from app.stitcher import _DOWNLOAD_HEADERS, download
from app.uploader import make_uploader
from app.video_gen import DEFAULT_MODEL_ID, VideoRequest, make_video_client
from app.voice import (
    Alignment,
    apply_respellings,
    decaps_emphasis,
    make_voice_client,
    voice_for,
)

ROOT = Path(__file__).resolve().parent.parent

# Per-provider TTS fan-out cap. Each service bills/limits CONCURRENT requests
# and 429s past the plan limit, so the isolated per-beat fan-out is bounded by
# whichever provider is actually speaking.
_TTS_CONCURRENCY = {
    "elevenlabs": ELEVENLABS_MAX_CONCURRENCY,
    "cartesia": CARTESIA_MAX_CONCURRENCY,
    "fish": FISH_MAX_CONCURRENCY,
}

# Where the UI writes workflow runs (app/ui.py builds run dirs under this very
# constant). Anchored on ROOT so resume works regardless of the process's cwd —
# a cwd-relative run dir would be invisible to resumable_runs() below.
WORKFLOWS_ROOT = ROOT / "jobs" / "workflows"

# Seedance integer-second duration bounds. Windows are ≤5s (plan-time word
# caps: ≤8 words/beat + one held ≤11; runaways chopped by split_long_windows
# at max_s=5.0 into ~3s jump cuts), so a clip should never exceed ~5-6s —
# _DUR_MAX is a SOFT cap used only to warn. The request itself must ALWAYS
# cover the window (a clip shorter than its window can't be trimmed and kills
# the run at assembly — after every clip was already billed), so it clamps to
# the API max instead. The 4s FLOOR is Seedance's documented minimum.
_DUR_MIN, _DUR_MAX = 4, 6
_DUR_API_MAX = 15

# Talking-head lipsync routes accepted by generate_ugc_video. Checked before
# any client is built — a typo'd engine must fail before a single paid call.
_LIPSYNC_ENGINES: frozenset[str] = frozenset(
    {"syncso", "heygen", "seedance_native", "heygen_replicate"}
)

# Text-LLM providers accepted by generate_ugc_video. Same fail-fast rule:
# a bad provider (or claude without its key) must die before any paid call.
_LLM_PROVIDERS: frozenset[str] = frozenset({"claude", "gemini"})


def _requested_duration(window_len: float) -> int:
    """Seedance integer duration for a beat window: always ≥ the window
    (trim needs surplus, never deficit), floored at the API minimum, capped
    at the API maximum — NEVER at the soft cap (that would knowingly order a
    clip too short for its own window)."""
    return max(_DUR_MIN, min(_DUR_API_MAX, math.ceil(window_len)))


def _omni_duration(window_len: float) -> int:
    """Omni integer duration for a beat window — its OWN 3-10s API range.

    Deliberately not _requested_duration: those 4..15 bounds are Seedance's, and
    sending 15 here would be rejected (Omni's ceiling is 10s) while a 4s floor
    would over-bill the 3s beats. _omni_windows already caps every window at
    OMNI_DUR_MAX, so the clamp is a guard, not the normal path.

    Carries OMNI_SPEECH_HEADROOM_S above the window. The window is the PLANNED
    speech length at an average rate; Omni's actual rate varies (1.89-2.92 w/s
    measured), and a beat that runs slower than planned has its line cut off
    mid-sentence on a clip nothing downstream can repair. The headroom buys that
    slack in video we may not use — assembly's speech-end trim removes it from
    the cut, so it costs a little money and never costs pacing.
    """
    return max(OMNI_DUR_MIN,
               min(OMNI_DUR_MAX,
                   math.ceil(window_len + OMNI_SPEECH_HEADROOM_S)))


def _omni_windows(
    planner_beats: list, *, product_ref_url: str | None = None,
    product_name: str | None = None,
    on_progress: "Callable[[str], None]" = print,
) -> tuple[list, list[tuple[float, float]], int]:
    """Synthetic beat windows for omni mode — the clock with no voiceover.

    Omni authors the speech itself, so there is no VO to force-align and no
    ground-truth clock before the clips exist. Windows are minted from each
    beat's word budget at OMNI_WORDS_PER_SECOND. They only need to be right on
    average: the clip REQUEST is sized from the window, and the real timeline
    is recovered per clip at assembly (speech-end trim keeps everything the
    model actually said; captions force-align against the finished video).

    Mirrors the shape of the beat_windows → split_long_windows chain so every
    downstream consumer (manifest schema, resume, assemble) sees the same
    contract:
      - beats whose estimate exceeds OMNI_SHOT_MAX_S are word-chunked into equal
        jump-cut sub-beats of ~OMNI_SHOT_TARGET_S (each re-spoken clip must carry
        ONLY its chunk) — the same fast-cut bar split_long_windows applies on the
        seedance path, which this function did not have before 2026-08-05;
      - every window is clamped into [OMNI_DUR_MIN, OMNI_DUR_MAX] — the API's
        own range, so the clip request never has to be re-sized against it;
      - reaction/silent beats get their hold as a silent window, capped at the
        same shot ceiling: a "hold" is still a shot, and a 10s one hangs on a
        single frame no matter how it was authored.

    Deliberately does NOT run merge_short_windows: that merge keeps only the
    dominant beat's fields, and in omni the beat's prompt is the ONLY place its
    line is spoken (there is no continuous VO underneath to cover the merged
    span), so a merge would delete an approved line from the ad — and from the
    captions, which are force-aligned off these same beats. A sub-floor talking
    beat instead gets a full OMNI_DUR_MIN window: identical billing to what its
    own clip would cost anyway, and its words survive.

    Returns (beats, windows, n_split) with beats reindexed + sub_index-stamped
    exactly like split_long_windows.
    """
    def _clamp(seconds: float) -> float:
        return max(float(OMNI_DUR_MIN), min(float(OMNI_DUR_MAX), seconds))

    est_beats: list = []
    est_lens: list[float] = []
    n_split = 0
    n_floored = 0
    for b in planner_beats:
        mode = resolve_voice_mode(b)
        if mode == "silent":
            hold = float(getattr(b, "hold_s", None) or 0.0)
            est_beats.append(copy.copy(b))
            # A hold is a SHOT, so it answers to the shot ceiling, not the API
            # ceiling: an 8s "hold on the product" is exactly the dead frame the
            # fast-cut bar exists to prevent. Clamped here rather than at the
            # clip request because the window IS what assembly cuts to.
            est_lens.append(min(_clamp(hold or float(OMNI_DUR_MIN)),
                                OMNI_SHOT_MAX_S))
            continue
        words = _strip_tags(b.dialogue_text or "").split()
        est = len(words) / OMNI_WORDS_PER_SECOND
        # The shot ceiling is enforced on SHOTS, unconditionally and before any
        # grouping: divide first, merge second. Multi-shot changes only how the
        # resulting shots are PACKAGED (several to a take, dialogue running
        # through), never whether a 10s span is allowed to sit on one frame —
        # which is what leaving it whole for the grouper to "cut internally" did.
        if est <= OMNI_SHOT_MAX_S:
            if est < OMNI_DUR_MIN:
                n_floored += 1
            est_beats.append(copy.copy(b))
            est_lens.append(_clamp(est))
            continue
        # Over the shot ceiling: equal word-chunks, one sub-beat per chunk —
        # same dialogue-splitting contract as split_long_windows (a re-spoken
        # clip must never re-utter the whole line).
        #
        # Chunks are allowed to fall UNDER OMNI_DUR_MIN. That 3s floor is the
        # provider's billing/API minimum, not a pacing one: a 2.5s chunk is
        # ordered as a 3s clip and the speech-end trim cuts the shot back to the
        # 2.5s actually spoken, which is the fast cut we want. Refusing to split
        # below the floor is what left 5.5s shots on screen (a 5.5s line cannot
        # make two ≥3s halves), so the floor buys video, never screen time.
        k = math.ceil(est / OMNI_SHOT_TARGET_S)
        n_split += k - 1
        for j in range(k):
            sub = copy.copy(b)
            chunk = " ".join(words[j * len(words) // k:(j + 1) * len(words) // k])
            try:
                object.__setattr__(sub, "dialogue_text", chunk)
                object.__setattr__(sub, "sub_index", j)
                object.__setattr__(sub, "_shot_parent", id(b))
            except Exception:
                pass
            est_beats.append(sub)
            est_lens.append(_clamp(est / k))
    cursor = 0.0
    windows: list[tuple[float, float]] = []
    for length in est_lens:
        windows.append((cursor, cursor + length))
        cursor += length
    if n_floored:
        # Not a fault: the clip bills OMNI_DUR_MIN either way, and assembly's
        # speech-end trim cuts the surplus back out of the final cut.
        on_progress(
            f"[omni] {n_floored} short talking beat(s) floored to "
            f"{OMNI_DUR_MIN}s windows"
        )
    if OMNI_MULTISHOT:
        est_beats, est_lens, n_grouped = _omni_group_shots(
            est_beats, est_lens, product_ref_url=product_ref_url,
            product_name=product_name)
        if n_grouped:
            on_progress(
                f"[omni] {n_grouped} beat(s) folded into multi-shot takes: the "
                f"scene cuts INSIDE the clip while the line keeps running"
            )
        cursor = 0.0
        windows = []
        for length in est_lens:
            windows.append((cursor, cursor + length))
            cursor += length
    for i, b in enumerate(est_beats):
        try:
            object.__setattr__(b, "index", i)
        except Exception:
            pass
    return est_beats, windows, n_split


def _omni_group_shots(beats: list, lens: list[float], *,
                      product_ref_url: str | None = None,
                      product_name: str | None = None,
) -> tuple[list, list[float], int]:
    """Fold runs of consecutive beats into single multi-shot generations.

    Returns (beats, lens, n_folded) where a folded beat carries its parts on
    ``shots`` + ``shot_lens`` and its own ``dialogue_text`` is their lines joined
    in order — so every downstream consumer that reads dialogue_text (captions,
    verbatim checks, the line-fidelity clause) still sees the whole spoken text.

    A group closes when the next beat is not groupable (product / hook / silent —
    see _omni_groupable), when adding it would pass OMNI_DUR_MAX (the hard clip
    ceiling on both routes), or when the group already holds the most shots one
    take should carry. Groups of one are left exactly as they were.
    """
    out_beats: list = []
    out_lens: list[float] = []
    n_folded = 0
    run: list = []
    run_lens: list[float] = []

    def _flush() -> None:
        nonlocal n_folded
        if not run:
            return
        if len(run) == 1:
            out_beats.append(run[0])
            out_lens.append(run_lens[0])
        else:
            merged = copy.copy(run[0])
            joined = " ".join((b.dialogue_text or "").strip() for b in run
                              if (b.dialogue_text or "").strip())
            for field, value in (("dialogue_text", joined),
                                 ("shots", list(run)),
                                 ("shot_lens", list(run_lens)),
                                 ("sub_index", 0)):
                try:
                    object.__setattr__(merged, field, value)
                except Exception:
                    pass
            out_beats.append(merged)
            out_lens.append(sum(run_lens))
            n_folded += len(run)
        run.clear()
        run_lens.clear()

    # A shot inside a take still answers to the fast-cut bar, so the most shots a
    # take can hold is however many target-length shots fit under the ceiling.
    max_shots = max(2, int(OMNI_DUR_MAX // OMNI_SHOT_TARGET_S))
    for b, ln in zip(beats, lens):
        if not _omni_groupable(
                b, uses_product=_beat_uses_product(
                    b, product_ref_url, product_name)):
            _flush()
            out_beats.append(b)
            out_lens.append(ln)
            continue
        # Only fold shots from DIFFERENT screenplay beats. Two shots carved out of
        # one beat share that beat's single scene, so folding them would build a
        # take whose blocks all describe the same thing while the shot-list clause
        # demands the scene change at every cut — a prompt that argues with
        # itself. There is no scene to change to: the screenplay never wrote one
        # for that span. They stay separate clips (jump cuts), and the fix for
        # that is more BEATS, not more folding.
        same_beat = (run and getattr(b, "_shot_parent", None) is not None
                     and getattr(b, "_shot_parent", None)
                     == getattr(run[-1], "_shot_parent", object()))
        if run and (same_beat
                    or sum(run_lens) + ln > OMNI_DUR_MAX
                    or len(run) >= max_shots):
            _flush()
        run.append(b)
        run_lens.append(ln)
    _flush()
    return out_beats, out_lens, n_folded


# EvoLink bills video per SECOND in credits (1 credit = $0.014722): seedance-2.0
# = 13.5 credits/s ($0.199/s), kling-v3 = 5.4 credits/s ($0.0795/s) — ~2.5x
# cheaper. Verified from both the EvoLink price list and our own billing history.
# The account runs on a prepaid credit balance, so every run logs what it spent.
_CREDITS_PER_SECOND: dict[str, float] = {"seedance": 13.5, "kling": 5.4}
_USD_PER_CREDIT = 0.014722

# Dollar-metered clip providers: 0 credits, priced straight in USD.
#   omni         — Google's own route, billed per output second against the
#                  Gemini API key with no credit balance in the middle.
#   omni_evolink — the SAME model resold by EvoLink, but token-metered rather
#                  than credit-metered, so it does not belong in the credits
#                  table either (a fabricated credit figure would print as if
#                  EvoLink had charged it).
_USD_PER_SECOND: dict[str, float] = {
    "omni": OMNI_USD_PER_SECOND,
    "omni_evolink": OMNI_EVOLINK_USD_PER_SECOND,
}


def _clip_cost_estimate(clip_records: dict[int, dict]) -> dict:
    """Per-provider spend estimate for a run's clips, from the manifest records.

    Bills on the ORDERED duration, not the finished cut: the trim down to the
    beat window happens locally, long after EvoLink charged for the full clip
    (that is the known 4s-floor waste). Records with an unknown provider are
    counted as seedance — the pre-Kling default and the pessimistic one.

    Two billing shapes coexist: credit-metered providers (EvoLink) and
    dollar-metered ones (omni). Both land in the same per-provider dict so the
    output shape — and every consumer of it — is unchanged; a dollar-metered
    provider simply reports 0 credits.
    """
    by_provider: dict[str, dict[str, float]] = {}
    for rec in clip_records.values():
        provider = rec.get("provider") or "seedance"
        secs = float(rec.get("duration_requested") or 0)
        credits = secs * _CREDITS_PER_SECOND.get(provider, 0.0)
        agg = by_provider.setdefault(
            provider, {"clips": 0, "seconds": 0.0, "credits": 0.0, "usd": 0.0})
        agg["clips"] += 1
        agg["seconds"] += secs
        agg["credits"] += credits
        agg["usd"] += (credits * _USD_PER_CREDIT
                       + secs * _USD_PER_SECOND.get(provider, 0.0))
    return {
        "by_provider": by_provider,
        "total_credits": sum(v["credits"] for v in by_provider.values()),
        "total_usd": sum(v["usd"] for v in by_provider.values()),
    }


# The TimelineBeat fields resume needs to rebuild an effective beat. Snapshotted
# by name (not model_dump) so injected fakes / SimpleNamespace beats — tests and
# one-off scripts — snapshot identically, and so a new planner field can never
# silently change the on-disk resume record.
_BEAT_STATE_FIELDS: tuple[str, ...] = (
    "index", "ad_section", "dialogue_text", "shot_type", "framing",
    "broll_type", "broll_shot", "visual_note", "beat_kind", "hold_s",
    "shows_creator", "voice_mode",
)


def _beat_state(beat: Any) -> dict:
    """JSON-able snapshot of ONE effective (post-split/merge) beat.

    ``sub_index`` is read explicitly: split_long_windows stamps it with
    object.__setattr__, which pydantic's model_dump() does not report.
    """
    state = {f: getattr(beat, f, None) for f in _BEAT_STATE_FIELDS}
    state["sub_index"] = getattr(beat, "sub_index", 0)
    return state


def _rehydrate_beat(state: dict) -> Any:
    """Rebuild a TimelineBeat from a _beat_state snapshot (resume).

    Validating through the real model (rather than a SimpleNamespace) means a
    resumed run hands assemble()/_beat_uses_product exactly the object type a
    fresh run does. Extra keys (sub_index) are ignored by pydantic on the way
    in, then re-stamped the same way split_long_windows does.
    """
    beat = TimelineBeat.model_validate(state)
    object.__setattr__(beat, "sub_index", state.get("sub_index", 0))
    return beat


def _format_clip_spend(cost: dict) -> str:
    """One-line per-provider spend summary from _clip_cost_estimate.

    Dollar-metered providers (omni) print USD only — quoting "0 cr" for them
    would read as free. The trailing total keeps its shape for every run.
    """
    parts: list[str] = []
    for prov, v in sorted(cost["by_provider"].items()):
        head = f"{prov} {int(v['clips'])} clip(s) / {v['seconds']:.0f}s = "
        parts.append(head + (
            f"${v['usd']:.2f}" if prov in _USD_PER_SECOND
            else f"{v['credits']:.0f} cr (${v['usd']:.2f})"
        ))
    return (
        "; ".join(parts)
        + f" → total {cost['total_credits']:.0f} cr "
        f"(${cost['total_usd']:.2f})"
    )


def _clip_is_usable(path: str | Path) -> bool:
    """Is this local file a complete, playable clip?

    A killed download leaves a truncated (or zero-byte) mp4 behind, and reusing
    one silently poisons the concat, so presence is not enough — ffprobe must
    report a real duration. Uses assembly's ffprobe helper so resume validates
    clips with exactly the same probe the assembler will.
    """
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return False
    try:
        return _probe_duration(p) > 0
    except Exception:  # noqa: BLE001 — an unprobeable file is an unusable file
        return False


def _url_is_live(url: str, *, timeout: float = 30.0) -> bool:
    """Range-GET the first byte of an EvoLink result URL.

    A live task record is NOT proof the artifact is fetchable: EvoLink keeps the
    task row (status "completed" WITH its result url) long after the file itself
    expires. Retention was probed by age on the account's own tasks — 0/1/2-day-
    old urls answer HTTP 206, 4-day-old ones 404 (~72h) — so liveness is decided
    by bytes, never by the status field.

    Sends the browser UA: files.evolink.ai 403s the default Python-urllib agent
    (see app/stitcher._DOWNLOAD_HEADERS), which would read as "dead" for every
    still-live clip. Blocking — call it via asyncio.to_thread inside a fan-out.
    """
    req = Request(url, method="GET",
                  headers={**_DOWNLOAD_HEADERS, "Range": "bytes=0-0"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return 200 <= int(r.status) < 300
    except Exception:  # noqa: BLE001 — 404/403/timeout all mean "don't trust it"
        return False


def _gather_with_retry(factories, *, label, on_progress):
    """Run zero-arg async factories concurrently; retry failed indices once;
    raise only if any still fail after the retry.

    Shared by the anchor and clip stages: a single transient failure must not
    cancel the other in-flight (already-billed) calls, and a clean retry covers
    TLS/API blips without doubling cost on a real outage. ``factories[i]`` is a
    callable returning a fresh coroutine for index ``i``.
    """
    async def _run():
        results = await asyncio.gather(*[f() for f in factories],
                                       return_exceptions=True)
        failed = [i for i, r in enumerate(results) if isinstance(r, BaseException)]
        if failed:
            on_progress(
                f"retrying {len(failed)} failed {label}: {failed} "
                f"(waiting 10s for transient errors to clear…)"
            )
            await asyncio.sleep(10)
            retries = await asyncio.gather(*[factories[i]() for i in failed],
                                           return_exceptions=True)
            for i, r in zip(failed, retries):
                results[i] = r
        still = [(i, r) for i, r in enumerate(results)
                 if isinstance(r, BaseException)]
        if still:
            idxs = [i for i, _ in still]
            raise RuntimeError(
                f"{label} permanently failed for {idxs}: {still[0][1]!r}"
            ) from still[0][1]
        return results
    return _run()


def _assert_aligned(**named) -> None:
    """Raise if the named lists differ in length — guards the paid fan-out
    against a silent ``zip`` truncation that would drop beats with no error."""
    lengths = {k: len(v) for k, v in named.items()}
    if len(set(lengths.values())) > 1:
        raise RuntimeError(f"pipeline stage length mismatch: {lengths}")


def _seed_for(work, i: int) -> int:
    """Deterministic per-beat Seedance seed derived from the run's work dir.

    Same run + same beat → same seed (reproducible/inspectable); bumping it
    re-rolls a single clip without disturbing its siblings. Uses sha256 (not
    Python's salted hash()) so it's stable across processes.
    """
    base = int(hashlib.sha256(str(work).encode()).hexdigest()[:8], 16)
    return (base + i) % 2_147_483_647


class _Manifest:
    """Atomic-written JSON state for the workflow run.

    Updated at every milestone. If the pipeline dies mid-flight, this file
    holds every URL / path / id generated so far — so a paid-for EvoLink
    clip never disappears just because the laptop slept.
    """
    def __init__(self, path: Path):
        self._path = path
        self._data: dict = {
            "version": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "status": "in_progress",
            # Ordered ledger of major pipeline steps. Each entry pairs the
            # generation PROMPT (when the step has one) with what it PRODUCED
            # (output summary + artifact refs incl. S3 URLs). This is the
            # human-readable "what happened, in order" view; the typed keys
            # below (character/timeline/clips/...) remain for resume logic.
            "steps": [],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, path: str | Path) -> "_Manifest":
        """Rehydrate a run's manifest from disk (resume entry point).

        __init__ ALWAYS built a fresh _data dict and nothing ever read the file
        back, so every "resumable record" the pipeline wrote was write-only —
        this is the other half of that bug (see `get`). The persisted fields are
        kept verbatim, `version` included: a resume must not rewrite the record
        of what was already paid for.
        """
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"no manifest at {p} — nothing to resume"
            ) from e
        except json.JSONDecodeError as e:
            raise ValueError(f"manifest at {p} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(
                f"manifest at {p} is not a JSON object (got {type(data).__name__})"
            )
        m = cls(p)
        m._data = data
        return m

    def get(self, key: str, default: Any = None) -> Any:
        """Read a persisted field.

        Added 2026-07-25. Its absence was a silent correctness bug: the
        assemble() call site guarded with `hasattr(manifest, "get")`, so
        `prior_lipsync_records` was PERMANENTLY None and every talking head was
        re-lipsynced (re-paid) on every retry of a run.
        """
        return self._data.get(key, default)

    def update(self, **fields: Any) -> None:
        self._data.update(fields)
        self._data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def add_step(self, step: str, *, index: int | None = None,
                 prompt: str | None = None,
                 produced: Any = None) -> None:
        """Append one major step to the ordered `steps` ledger and persist."""
        self._data.setdefault("steps", []).append({
            "step": step,
            "index": index,
            "prompt": prompt,
            "produced": produced,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self._data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def mark(self, status: str, *, error: str | None = None) -> None:
        self._data["status"] = status
        if error is not None:
            self._data["error"] = error
        self._save()

    def path(self) -> Path:
        return self._path

    def _save(self) -> None:
        """Atomic write — write to a sibling tmp file, then rename."""
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(self._path.parent), prefix=".manifest-",
            suffix=".tmp", delete=False, encoding="utf-8",
        )
        try:
            json.dump(self._data, tmp, indent=2, default=str)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self._path)
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

async def _try_upload_deliverable(
    uploader: Any, local_path: str | Path, *,
    on_progress: Callable[[str], None],
) -> str | None:
    """Upload a finished media deliverable to S3, never failing the run.

    The local file is the source of truth (run files stay local); an S3 hiccup
    must not discard a paid render. Runs the blocking boto3 call off the event
    loop so it doesn't stall concurrent fan-outs. Returns the public URL, or
    None on any failure.
    """
    try:
        return await asyncio.to_thread(uploader.upload, str(local_path))
    except Exception as exc:  # noqa: BLE001 — S3 is best-effort, local wins
        on_progress(f"  S3 upload failed for {Path(local_path).name}: {exc!r}")
        return None


# B-roll shot types that actually feature the product. The other broll_type
# (lifestyle) is a creator/environment shot with NO product in frame.
_PRODUCT_BROLL_TYPES: frozenset[str] = frozenset({
    "product_closeup", "hands_using", "unboxing", "before_after",
    "texture_pour", "product_in_scene", "demo_steps",
})

# B-roll types that show the CREATOR'S hands — these carry the character ref
# (hands-only citation) so the hands match her skin tone / jewellery instead
# of rendering a random stranger's hands.
_HANDS_BROLL_TYPES: frozenset[str] = frozenset({
    "hands_using", "demo_steps", "texture_pour", "unboxing", "before_after",
})

# Talking-head framings shot from a propped/static rig — the ONLY ones that
# get Seedance's camera_fixed lock. Every other framing keeps deliberate
# handheld energy (slight natural sway is the UGC tell); locking them reads
# as studio footage. The taxonomy's resting-camera frames (on the floor / a
# table / overhead — docs/framing-taxonomy.md) are physically fixed rigs, so
# they lock too; pans/follows/drops must NOT (the move IS the frame).
_STATIC_CAMERA_FRAMINGS: frozenset[str] = frozenset({
    "tripod", "front_pano", "floor_shot", "low_angle_table", "closeup_chest",
    "found_it", "top_angle", "top_high_angle", "wide_side_low",
})

# Per-framing CAMERA MOTION line for talking-head motion prompts — defined in
# gemini.py next to the framing taxonomy (the Opus motion writer surfaces the
# same text); imported here for the deterministic f-string builders.
_FRAMING_MOTION_CAMERA_DEFAULT = (
    "Filmed on an iPhone front camera, handheld with a slight natural sway"
)

# Product/hands b-roll types (product macros + the hands-on-product shot). These
# take a quick, CONTROLLED push-in (_PRODUCT_PUSHIN_CAMERA) rather than a hard
# camera lock: locking the camera over their static subjects froze the clip
# (Seedance i2v ran out of motion — a 6s "held product" beat came out visibly
# stuck) and sometimes made it hallucinate a phantom hand over a hand-free
# frame. The scale-only push-in gives defined, energetic motion while
# _PRODUCT_STABILITY_CLAUSE keeps the product rigid (no morph). Also used to
# pick the hands-only citation. Lifestyle / no-product b-roll keeps its free
# handheld feel.
_PRODUCT_HANDS_BROLL_TYPES: frozenset[str] = frozenset({
    "product_closeup", "product_in_scene", "hands_using",
})


def build_timbre_anchor(vo_audio_path, work_dir, uploader, *,
                        seconds: float = 7.0) -> str:
    """Cut the first `seconds` of the (paced) VO as the run's ONE timbre
    fingerprint and upload it once. The same URL is passed as audio_url to
    every speaking clip — that is what keeps the native voice consistent.
    The VO itself never reaches the output (oracle + donor only)."""
    anchor = Path(work_dir) / "timbre-anchor.wav"
    slice_audio(vo_audio_path, 0.0, seconds, anchor)
    return uploader.upload(str(anchor))


def _camera_fixed(beat: Any) -> bool:
    """camera_fixed for a beat's Seedance request: lock ONLY static-rig talking
    heads (see _STATIC_CAMERA_FRAMINGS). B-roll NEVER locks — product/hands
    shots take a slow controlled push-in (a hard lock froze the footage and made
    i2v hallucinate a phantom hand), and lifestyle keeps its handheld feel. The
    product is held rigid by _PRODUCT_STABILITY_CLAUSE, not by a frozen camera."""
    if beat.shot_type == "talking_head":
        return beat.framing in _STATIC_CAMERA_FRAMINGS
    return False


def _anchor_visual_note(beat: Any) -> str:
    """Visual note feeding a beat's anchor prompt. Sub-beats after the first
    (sub_index > 0, set by split_long_windows) are mid-take jump cuts: their
    anchor must read as a CONTINUATION of the parent take — identical anchors
    snap every jump cut back to the same opening pose."""
    note = beat.visual_note
    if getattr(beat, "sub_index", 0) > 0:
        note = (
            f"{note} — mid-take continuation: caught mid-sentence, hands "
            f"mid-gesture, NOT the opening pose"
        )
    return note


# Product nouns that give a beat away when the planner forgot to flag it. Only
# consulted alongside a product-name match or a product broll_type — see
# _mentions_product — because "bottle" alone is as likely to be a water bottle.
_PRODUCT_NOUNS: tuple[str, ...] = (
    "product", "bottle", "jar", "tube", "sachet", "softgel", "capsule",
    "tablet", "pill", "strip", "serum", "dropper", "pouch", "pack", "packet",
    "box", "tin", "can", "label", "carton", "blister",
)


def _mentions_product(beat: Any, product_name: str | None) -> bool:
    """Does the beat's own screenplay text put the product on screen?

    Deterministic backstop for `product_in_frame`: the flag is authored by the
    planner, and a beat that describes the product but forgets the flag would
    otherwise be rendered with NO product reference — which is how a fake
    product gets invented (the one failure the product rule forbids).

    The product NAME is the reliable signal. Bare product nouns are NOT, and are
    used only when the run has no name to match on: measured against the real
    2026-08-05 script, the noun list produced two hits and both were wrong — the
    "strip of medicine tablets" on a problem-section counter is the ailment being
    described, not the product. So nouns are a last resort, never an override.

    Matching is word-boundaried. Substring matching had "tin" firing inside
    "hinting", which is how a talking beat with no product in it acquired a
    product reference.
    """
    text = " ".join(str(getattr(beat, f, "") or "") for f in
                    ("visual_note", "broll_shot")).lower()
    if not text.strip():
        return False
    words = [w for w in re.findall(r"[a-z0-9]+", (product_name or "").lower())
             if len(w) > 3]
    if words:
        return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)
    return any(re.search(rf"\b{re.escape(n)}s?\b", text) for n in _PRODUCT_NOUNS)


def _beat_uses_product(beat: Any, product_ref_url: str | None,
                       product_name: str | None = None) -> bool:
    """Should this beat be rendered WITH the real product reference attached?

    The rule is deliberately generous: whenever the product is on screen in ANY
    way — held, used, on a counter, on a shelf, in her hand, or just sitting in
    the background of a talking head — the real photo must be attached, or the
    model invents a lookalike with the wrong label. A false positive costs one
    extra reference image; a false negative ships a fake product (2026-08-06).

    Three sources, in order of authority:
      1. ``product_in_frame`` — the planner's own declaration. The screenplay
         decides what is in the shot, so its answer wins when it says yes.
      2. Structural: talking_head + framing=product_reveal_pivot, or a
         product-bearing broll_type. The pre-2026-08-06 rule, kept whole.
      3. The beat's own text naming the product (see _mentions_product) — the
         backstop for a planner that forgot the flag and for legacy manifests
         written before the field existed.
    """
    if not product_ref_url:
        return False
    if getattr(beat, "product_in_frame", False):
        return True
    if beat.shot_type == "talking_head":
        if beat.framing == "product_reveal_pivot":
            return True
    elif beat.broll_type in _PRODUCT_BROLL_TYPES:
        return True
    return _mentions_product(beat, product_name)


def _anchor_refs(
    beat: Any, char_ref_url: str, product_ref_url: str | None,
    product_name: str | None = None,
) -> tuple[list[str], list[str]]:
    """Reference image URLs + their citation roles for a beat's anchor, driven
    by shows_creator (the director's per-beat "is the creator on screen?"
    flag), NOT shot_type:
      - on screen + hands-only B-roll → cite the character HANDS-ONLY so the
        hands match the creator (skin/nails/jewellery) but no face renders;
      - on screen otherwise (talking head or person-in-scene B-roll) → the
        full character ref;
      - NOT on screen (pure product macro / no-person scene) → no character
        ref at all, so the model never invents a stranger.
    The product ref is appended (role "product") whenever _beat_uses_product.
    Roles are what the image client cites from — a product-only beat must be
    cited as the PRODUCT, never "image 1 is the character".
    """
    refs: list[str] = []
    roles: list[str] = []
    if getattr(beat, "shows_creator", True):
        # A product shot (product_closeup/product_in_scene) that shows the
        # creator shows her HANDS holding the product, never her face — so it
        # gets the hands-only citation too. Without this, a product_closeup
        # fell to the full-"character" branch and the model rendered a stranger
        # (a man's hairy hand on the bottle, run v2-20260614-001634).
        hands_only = (beat.broll_type in _HANDS_BROLL_TYPES
                      or beat.broll_type in _PRODUCT_HANDS_BROLL_TYPES)
        refs.append(char_ref_url)
        roles.append("character_hands" if hands_only else "character")
    if _beat_uses_product(beat, product_ref_url, product_name):
        refs.append(product_ref_url)   # type: ignore[arg-type]
        roles.append("product")
    return refs, roles


# Branded products hallucinate under text-to-image, so the user's photo is run
# through a Gemini EDIT (cited as "product_source") into a clean bottle-only
# canonical reference: pixel-accurate label/logo/text, no packaging. One retry
# keeps a transient blip from costing the run; a permanent failure returns None
# so the caller falls back to the original photo rather than killing the run.
_PRODUCT_NORMALIZE_PROMPT = (
    "Isolate the product on a clean, plain neutral background. Remove all "
    "outer packaging, boxes, and clutter. Keep the product's label, logo, and "
    "text unchanged — do not alter, redraw, or reposition any branding, and "
    "never print any words from these instructions inside the image."
)


async def _normalize_product_ref(
    image_client: Any, product_ref_url: str, *,
    on_progress: Callable[[str], None],
) -> str | None:
    for _attempt in range(2):
        try:
            imgs = await image_client.generate_async(
                prompt=_PRODUCT_NORMALIZE_PROMPT, tier="hero", source="workflow",
                reference_image_urls=[product_ref_url],
                reference_roles=["product_source"],
            )
            return imgs[0].url
        except Exception as exc:  # noqa: BLE001
            on_progress(f"  product normalization attempt failed: {exc!r}")
    return None


# Anatomy-stability clause appended to every Seedance motion prompt. Seedance
# can warp hands/fingers DURING motion even when the start frame is clean, so
# we (a) constrain anatomy explicitly and (b) bias toward minimal hand motion
# (the single biggest glitch source). The planner additionally schedules
# B-roll over the riskiest moments.
_ANATOMY_CLAUSE = (
    "Exactly one pair of hands, one person, five fingers each — natural length, "
    "defined knuckles, never stretched, fused, warped, rubbery, extra, or "
    "phantom; no finger points at the lens; small gestures fully inside the "
    "frame; face and body proportions identical to the first frame."
)

# Fine-motor beats (tying/knotting/threading/braiding — gemini._DEXTERITY_RE):
# the MOMENT stays in the ad, the RENDERING is constrained. Animating the full
# multi-step finger coordination warps fingers and bends the object
# (user-reported: lace-tying), but the anchor already shows the hands
# mid-action and a cut-on-action insert reads as the whole action. Force-
# appended on matching beats, same rule as the anatomy guard.
_FINE_MOTOR_CLAUSE = (
    "The hands stay in their mid-action position and make ONE small, decisive "
    "movement — the intricate multi-step finger work never plays out on "
    "camera; the moment reads through the pose and the cut, landing "
    "mid-action like real UGC."
)

# Product/hands shots only: the grip and the product itself must not drift
# across the clip. Stacked onto the product b-roll prompt so the label stays
# pixel-stable and the held object keeps its exact shape under motion.
_PRODUCT_STABILITY_CLAUSE = (
    "The product keeps its exact shape, proportions, colour, label and "
    "real-world SIZE relative to the scene throughout — it never independently "
    "grows, shrinks, stretches, or rescales, and never morphs or warps (a "
    "camera push-in may enlarge the whole frame, but the product itself must "
    "not change size on its own). Its label and every piece of printed text "
    "stay printed flat and fixed on the product's own surface, moving rigidly "
    "with the product — never floating, sliding, peeling, detaching, or "
    "hovering off the surface, including on transparent, glass, or clear "
    "see-through packaging. The grip stays constant."
)

# Product/hands HERO shots: a held product invites Seedance to "perform" — it
# raises a second hand, re-grips, or strokes the bottle (the user saw a second
# hand creep on and "caress" it). The cure is to demand a NEAR-FROZEN hold, not
# an action: the product is ALREADY in position and simply stays there. Used in
# place of _SETTLE_CLAUSE for product/hands b-roll (settle implies an action to
# complete, which is exactly what spawns the extra hand).
_PRODUCT_HOLD_CLAUSE = (
    "The product moves only as the single deliberate action this shot calls for "
    "(a confident lift, one quick turn to the label, planting it down with "
    "intent) — decisive and quick, never sluggish or drawn out; controlled, not "
    "frantic. The cap/lid stays ON exactly as it starts: do NOT "
    "spray, spritz, or press the nozzle (the closed product must never be shown "
    "spraying). NO additional hand reaches in, no stroking or caressing, and the "
    "grip stays controlled throughout."
)

# Product beats only: Seedance can hallucinate a SECOND copy of the product
# drifting in from a frame edge even when the anchor shows exactly one
# (QC-confirmed on run v2-20260710-005358: duplicate bottle at a corner while
# the creator holds the real one). Named object-count constraint, phrased
# affirmatively.
_SINGLE_PRODUCT_CLAUSE = (
    "Exactly ONE product bottle is in frame for the entire clip — never a "
    "second, duplicate, or reflected copy of the product, and nothing new "
    "enters from any frame edge."
)

# Unheld product (product_in_scene): the product SITS in the scene. The hold
# clause assumes a hand and thereby INVITES Seedance to add one (QC-confirmed:
# disembodied hands reached into a resting-product shot). Demand stillness
# and an explicitly hand-free frame instead.
_PRODUCT_REST_CLAUSE = (
    "The product stays resting exactly where it sits in the first frame — it "
    "is never picked up, and no hand or arm enters the frame."
)

# B-roll only. Previously demanded a frozen "held framing for the final
# second" so the speech-derived trim never caught a mid-swing — but at fast-cut
# shot lengths (2-3s) that froze ~a third of the clip and read as "stuck"
# (user-reported). Real UGC cuts ON action, so the clause now demands
# real-time energy through the whole clip and merely bans freezing. NOT used
# for talking heads (sync-3 needs visible speaking motion throughout).
_SETTLE_CLAUSE = (
    "The action plays out at real-time energy through the whole clip — it is "
    "fine for the cut to land mid-motion like real UGC footage; the subject "
    "never freezes or stops moving."
)

# Skin micro-texture + light sensor optics. In i2v, frame 1 IS the anchor still
# but frames 2..N are RE-SYNTHESIZED and regress toward a smooth "beauty" prior
# unless the texture is NAMED — pores/vellus-hair/grain read as noise to the
# denoiser and get averaged away in silence. Naming them is what DEFENDS the
# realism the still already earned; it was the single biggest cause of the waxy
# look and was specced but never shipped (see docs/seedance-realism-guide.md).
# Placed EARLY in each prompt (token weight decays toward the tail), and phrased
# affirmatively: Seedance has NO negative_prompt field, and "no plastic skin"
# can BACKFIRE by surfacing the very noun it negates. Kept FLATTERING — these are
# attractive influencers, so it asks for clear healthy skin with *fine* real
# texture (just enough to break the waxy CGI sheen), NOT pores/oil/peach-fuzz
# emphasis that would make a beautiful creator read as blotchy or greasy.
_SKIN_TEXTURE_CLAUSE = (
    "Clear, healthy, even-toned attractive skin with fine natural texture and "
    "gentle subsurface scattering — a clean, matte, non-shiny finish, never "
    "oily or sweaty; no blemishes, blotchiness, redness or grime; realistic, "
    "never plastic or dirty."
)

# Product/hero b-roll texture: a clean label is dirtied by film grain, so the
# static-product path uses material realism INSTEAD of the grain tail.
_PRODUCT_TEXTURE_CLAUSE = (
    "Realistic material reflections and natural surface texture, photorealistic."
)

# First-frame faithfulness: stops Seedance re-painting / morphing the still as
# it animates. Attested wording only ("preserve composition and colors") —
# lighting is owned by the (now even/diffuse) light clause, not bolted on here.
_FAITHFULNESS_CLAUSE = (
    "Preserve the composition and colors of the reference image."
)

# Product/hands b-roll camera. ONE quick decisive push-in: a hard camera lock
# froze the clip (Seedance ran out of motion), and the old "very slow" push-in
# read as a stuck frame creeping for 5s (user-reported). Quick-but-smooth keeps
# defined energetic motion while staying a scale-only move (no orbit/pan/
# parallax) so _PRODUCT_STABILITY_CLAUSE can still hold the label rigid.
_PRODUCT_PUSHIN_CAMERA = (
    "The camera makes one quick, smooth, confident push-in — a clean scale "
    "only, no orbit, pan, or parallax — motion never freezing or holding a "
    "static frame"
)


# Per-ad-section delivery tone, so consecutive talking heads don't all move
# the same way (the old single template gave every beat identical head-bobs).
_SECTION_MOTION: dict[str, str] = {
    "hook": "leaning slightly toward the lens with bright, attention-grabbing energy",
    "problem": "a small concerned head-tilt and a relatable, slightly exasperated expression",
    "solution": "a brightening, discovering expression and easy forward energy",
    "proof": "confident and matter-of-fact with easy nods and a knowing half-smile",
    "cta": "warm, direct and settled, with a friendly final nod",
}

# Index-rotated micro-gesture so even two beats in the SAME section differ.
# Face/head only — hands are the #1 Seedance warp source, so no hand moves.
_MICRO_GESTURES: list[str] = [
    "a quick eyebrow raise on the key word",
    "a subtle head shake then a nod",
    "a brief glance away and back to the lens",
    "a light one-shoulder shrug",
]


def _talking_motion_prompt(beat: Any, window_len: float) -> str:
    # Seedance single-image I2V is biased toward static output (it "locks onto"
    # the still reference), so we lead with the UGC/iPhone trigger and demand
    # continuous real-time motion through the FULL clip — both so the talking
    # head reads as lively and so sync-3 has real mouth motion to correct
    # against. Per video-seedance-skill.md: ≤100 words, positives, camera and
    # subject in separate sentences. Motion is VARIED per beat (ad_section tone
    # + index-rotated micro-gesture) so the cut sequence doesn't read robotic.
    # The clip is ordered at ceil(window) but trimmed to the window, so
    # gestures must begin AND resolve inside the first window_len seconds or
    # the cut lands mid-swing. No settle clause here — freezing the tail
    # kills the lipsync. This is the Sync.so-mode prompt; the audio-driven
    # path uses _native_talking_prompt. The scene/wardrobe is carried by the
    # anchor IMAGE, not this motion prompt: re-describing what the anchor
    # already encodes makes Seedance pop props in / morph, so visual_note is
    # deliberately absent here — only motion + camera + what stays static.
    section = getattr(beat, "ad_section", "solution") or "solution"
    tone = _SECTION_MOTION.get(section, _SECTION_MOTION["solution"])
    micro = _MICRO_GESTURES[(getattr(beat, "index", 0) or 0) % len(_MICRO_GESTURES)]
    # The camera line follows the beat's FRAMING (docs/framing-taxonomy.md) —
    # one hard-coded selfie camera for every framing was a top cause of
    # same-looking videos.
    cam = _FRAMING_MOTION_CAMERA.get(getattr(beat, "framing", None) or "")
    # The "phone footage" trigger phrase stays in every variant — it is the
    # UGC-realism lever (see video-seedance-skill.md), not decoration.
    cam = (f"Real UGC phone footage. {cam}" if cam
           else _FRAMING_MOTION_CAMERA_DEFAULT)
    # Lighting is EVEN/DIFFUSE with steady exposure (not "soft daylight"):
    # directional/variable light makes the re-synthesized skin "breathe"/shimmer
    # across frames, a core plastic tell. _SKIN_TEXTURE_CLAUSE sits EARLY (high
    # token weight); faithfulness + anatomy guard the tail.
    return (
        f"{cam}, "
        "even soft diffuse daylight, steady exposure. "
        f"{_SKIN_TEXTURE_CLAUSE} "
        f"The creator talks to the lens the whole time, {tone}, with {micro}; "
        # Mouth stays SUBTLE (sync-3 re-renders the lips; big source jaw swings
        # bleed through as mismatched articulation — QC, run v2-20260714-142306)
        # but the overall presence is lively: the slow feel came from the body,
        # not the mouth.
        "speaking with subtle natural mouth movement and natural blinks — "
        "lively, energetic presence, never sluggish. "
        f"Gestures begin and resolve within the first {window_len:.1f} seconds, "
        "motion continuous at real-time speed for the full clip. "
        f"{_FAITHFULNESS_CLAUSE} {_ANATOMY_CLAUSE}"
        + (f" {_SINGLE_PRODUCT_CLAUSE}"
           if getattr(beat, "framing", None) == "product_reveal_pivot" else "")
    )


_FIDELITY_CLAUSE = (
    # "says exactly" already appears in the line-quoting sentence, so this clause
    # only needs to carry the NOTHING-else guard (the anti-padding lever).
    "She says the quoted line and NOTHING else — no added, improvised or "
    "repeated words; only ambient room tone between phrases."
)

# Pace is set by the prompt, NOT a numeric param (probe 2026-06-30: a brisk +
# explicit-rest instruction makes r2v speak at natural rate then hold the mouth
# closed, instead of stretching the speech to fill the ordered clip duration —
# which is what made the first native run sound slow).
#
# The RATE half of these clauses now lives in the director's voice_direction
# (whose pace slot is constrained to brisk/real-time variants), so only the
# unique half — the explicit REST after the line, which is the actual anti-stretch
# lever — is kept here. BytePlus warns that long prompts get "scattered" with
# details ignored, so duplicating cadence in two places costs accuracy elsewhere.
_NATIVE_PACE_CLAUSE = (
    # "No slow motion" guards the VIDEO (r2v sometimes renders slow-mo); the
    # SPEECH rate is the director's voice_direction pace slot.
    "No slow motion; after the line she holds a calm, closed-mouth resting "
    "expression."
)
_NATIVE_VO_PACE_CLAUSE = (
    "Never slowed or drawn out; after the line, only ambient room tone."
)

# ── Native VOCAL delivery direction ──────────────────────────────────────────
# In seedance_native the VIDEO MODEL synthesizes the speech, so the only prosody
# it ever gets is what the prompt says. Until now the prompt carried no vocal
# emotion direction at all — identity (render_voice_persona), rate
# (_NATIVE_PACE_CLAUSE), and a per-section tone that describes the FACE
# ("head-tilt", "eyebrow raise"), never the voice. A model given no delivery
# direction reads flat, which is exactly what it did.
#
# BytePlus documents a tri-slot delivery form for the Seedance prompt guide:
#   "In a [emotional state] emotional state, with a [tone] tone and a
#    [speaking pace] speaking pace, say: ..."
# The vocabulary below is drawn from their own worked examples (emotional state:
# calm/gentle/restrained/confident/hesitant/impatient; tone: even/soft/low/
# light-upbeat/refined/playful; pace: normal/slow/very slow/moderate/fast-paced).
# NOTE: this form is documented on the Seedance 1.5-pro guide and is NOT restated
# for 2.0 — carry-over is inferred, so this is the first thing to A/B if native
# delivery still reads wrong.
#
# Per ad section, matched to _SECTION_MOTION's visual intent so voice and face
# agree rather than fight.
# The DIRECTOR decides delivery, per line — see TimelineBeat.voice_direction.
# A static ad_section -> delivery table was rejected: it gives every hook the
# same read regardless of what the line actually says, while the planner already
# owns the emotional arc, the hook type, and the words themselves.
#
# This fallback exists only for beats planned before the field existed (resumed
# runs, older manifests). It is deliberately generic — one neutral, BRISK line
# rather than a lookup table pretending to be direction.
_VOICE_DIRECTION_FALLBACK = (
    "In an engaged state, with a warm, natural tone and a brisk, real-time pace"
)


def _native_voice_direction(beat: Any) -> str:
    """The director's vocal delivery direction for this beat.

    Empty/missing → the generic brisk fallback, never a per-section guess.
    """
    return (getattr(beat, "voice_direction", "") or "").strip() \
        or _VOICE_DIRECTION_FALLBACK


def _native_spoken_line(beat: Any, pronunciations: Any = None) -> str:
    """The line as Seedance should SPEAK it.

    Two transforms, both because Seedance is the TTS here:
      * respell the planner's flagged words — BytePlus's own documented fix for
        mispronunciation is exactly this homophone respelling (they note it is
        "only an optimization method" and cannot fix every word),
      * de-CAPS emphasis words — the docs are SILENT on how case inside a
        dialogue string is treated, which makes ALL-CAPS undefined behaviour and
        a plausible cause of spelled-out or shouted readings.
    Captions are unaffected: they come from dialogue_text via forced alignment.
    """
    line = _strip_tags(beat.dialogue_text or "").strip()
    line = decaps_emphasis(line)
    if USE_PRONUNCIATION_FIX and pronunciations:
        line = apply_respellings(line, pronunciations)
    return line


def _native_talking_prompt(beat: Any, persona_clause: str = "",
                           pronunciations: Any = None) -> str:
    # TIMBRE reference (not content-follow): the shared anchor rides as Audio 1
    # purely as a voice fingerprint; the LINE comes from the prompt. Strict
    # fidelity clause guards against the model's tendency to pad spare time
    # with off-script filler. Scene/wardrobe stay in the anchor IMAGE.
    # Deliberately NO talking-motion clause: the sync_talk gentle-mouth
    # direction exists only to give sync-3 motion to correct against and
    # would fight the reference audio. No gesture-window budget either — the reference audio
    # paces the take and the native trim runs to the end of speech, not a
    # hard window cut. [Audio tags] are spoken-instruction tokens, not speech
    # — stripped, or Seedance mouths "[laughs]". ≤100 words per the skill.
    section = getattr(beat, "ad_section", "solution") or "solution"
    tone = _SECTION_MOTION.get(section, _SECTION_MOTION["solution"])
    line = _native_spoken_line(beat, pronunciations)
    voice_dir = _native_voice_direction(beat)
    cam = _FRAMING_MOTION_CAMERA.get(getattr(beat, "framing", None) or "")
    cam = (f"Real UGC phone footage. {cam}" if cam
           else "Filmed on an iPhone front camera, handheld with a slight "
                "natural sway")
    return (
        f"{cam}, even soft diffuse daylight on the face. "
        f"{_SKIN_TEXTURE_CLAUSE} "
        f"{voice_dir}, the creator looks at the lens and says exactly: "
        f'"{line}" — {tone}. '
        f"{persona_clause} Her voice timbre references Audio 1. "
        f"{_NATIVE_PACE_CLAUSE} {_FIDELITY_CLAUSE} {_FAITHFULNESS_CLAUSE} "
        f"{_ANATOMY_CLAUSE}"
        + (f" {_SINGLE_PRODUCT_CLAUSE}"
           if getattr(beat, "framing", None) == "product_reveal_pivot" else "")
    )


def _native_vo_broll_prompt(beat: Any, persona_clause: str = "",
                            pronunciations: Any = None) -> str:
    # Off-screen voiceover over a cutaway: no lip-sync, the voice keeps flowing
    # in the SAME anchor timbre while the action plays.
    line = _native_spoken_line(beat, pronunciations)
    shot = (getattr(beat, "broll_shot", None) or beat.visual_note or "").strip()
    pushin = getattr(beat, "broll_type", None) in _PRODUCT_HANDS_BROLL_TYPES
    camera = (_PRODUCT_PUSHIN_CAMERA if pushin else
              "Natural lively handheld camera movement at real-time pace")
    # Product beats: faithfulness + product-lock FRONT-LOADED (Seedance
    # down-weights the tail, and the planner's shot text — written without ever
    # seeing the product photo — must never outrank the reference image).
    # Lifestyle beats carry the skin-texture defense instead (was missing here:
    # verified unwired 2026-07-31 — native b-roll skin went waxy).
    lead = f"{_FAITHFULNESS_CLAUSE} {_PRODUCT_STABILITY_CLAUSE} " if pushin \
        else f"{_SKIN_TEXTURE_CLAUSE} "
    tail = _PRODUCT_TEXTURE_CLAUSE if pushin else _FAITHFULNESS_CLAUSE
    return (
        f"{lead}{shot}. {camera}. "
        f'Off-screen, the same creator\'s voice continues as voiceover, saying '
        f'exactly: "{line}". {persona_clause} The voiceover timbre references '
        f"Audio 1. {_NATIVE_VO_PACE_CLAUSE} {_FIDELITY_CLAUSE} "
        f"{tail} {_ANATOMY_CLAUSE}"
    )


def _native_silent_broll_prompt(beat: Any) -> str:
    # Pure action / hold — no speech at all, only ambient/SFX.
    shot = (getattr(beat, "broll_shot", None) or beat.visual_note or "").strip()
    pushin = getattr(beat, "broll_type", None) in _PRODUCT_HANDS_BROLL_TYPES
    camera = (_PRODUCT_PUSHIN_CAMERA if pushin else
              "Natural lively handheld camera movement, phone-camera feel")
    # Same lead/tail split as _native_vo_broll_prompt: product beats get the
    # reference-first product lock, lifestyle beats get the skin-texture
    # defense (verified unwired here 2026-07-31).
    lead = f"{_FAITHFULNESS_CLAUSE} {_PRODUCT_STABILITY_CLAUSE} " if pushin \
        else f"{_SKIN_TEXTURE_CLAUSE} "
    tail = _PRODUCT_TEXTURE_CLAUSE if pushin else _FAITHFULNESS_CLAUSE
    return (
        f"{lead}{shot}. {camera}. No speech and no voiceover — ambient room "
        f"tone and natural sound effects only. "
        f"{tail} {_ANATOMY_CLAUSE}"
    )


# ── Omni prompt vocabulary ───────────────────────────────────────────────────
# Omni (gemini-omni-flash-preview) authors the video AND its audio from the
# prompt alone: there is no reference audio, no lipsync pass and no
# negative_prompt field, so identity, cadence, the spoken words and every ban
# all ride this one string. It rewards a full narrative brief (see
# video-omni-skill.md), which is why these builders read as briefs rather than
# as the terse motion fragments the i2v-only path uses.
#
# The clauses below are the SAFETY-CRITICAL tokens: they are re-appended
# verbatim after the Opus rewrite (same rule as the anatomy guard) because each
# one guards against a documented default failure.

# Omni's default is to cut its own multi-shot mini-montage — this single
# sentence is the highest-leverage line in any UGC prompt. Kept without a
# trailing period so it can open the scene sentence or be appended as its own.
_OMNI_SINGLE_SHOT_CLAUSE = (
    "In a single unbroken continuous shot, no scene cuts"
)

# Audio generates by default and invents a score if unattended. Speech has to
# dominate a UGC ad, so every speaking/VO clip bans the extras explicitly.
_OMNI_NO_MUSIC_CLAUSE = "No background music, no extra sound effects."

# There is no negative_prompt field, so the bans live in the positive prompt.
_OMNI_NO_TEXT_CLAUSE = "No on-screen text, no logo, no captions."

# The anchor image carries identity — but only if the prompt DECLARES what the
# image is (silence about it lets the model drift the wardrobe; measured).
_OMNI_CHAR_REF_CLAUSE = (
    "Use the provided image as the exact character reference — preserve the "
    "face, hairstyle, wardrobe, jewellery and room exactly."
)
_OMNI_SCENE_REF_CLAUSE = (
    "Use the provided image as the exact opening frame and reference for this "
    "shot — preserve the setting, surfaces, props and colours exactly."
)
_OMNI_PRODUCT_REF_CLAUSE = (
    "The product in frame is the exact product reference — keep its shape, "
    "material, colours, label and logo placement identical."
)

# Anti-padding: a generated voice will happily improvise past the script, and a
# clip that says something we never wrote is a dead paid clip.
_OMNI_LINE_FIDELITY_CLAUSE = (
    "The creator speaks only the quoted line, word for word — no added, "
    "improvised or repeated words."
)

# UGC delivery is brisk. Measured on the 2026-08-05 run, Omni read these lines at
# 1.89-2.92 words/sec — the slow end drags, and it is the slow end that ran out of
# video mid-sentence. This asks for the top of that range in the model's OWN
# terms (an energetic read), never for a rate in words/sec, which Omni does not
# honour numerically. It does NOT ask for a rushed or clipped delivery: the ad
# still has to sound like a person talking to a friend.
# ─── in-clip multi-shot ───────────────────────────────────────────────
# The shot list comes from the SCREENPLAY, one timecode block per beat, each with
# that beat's own scene. Omni changes scene between its own cuts natively, so
# there is nothing to work around: the brief and the screenplay decide what each
# shot shows, and Omni executes the change. Cuts are real scene changes, not
# reframes of one room.
#
# This is the picture's counterpart of _OMNI_LINE_FIDELITY_CLAUSE: follow the
# list exactly, in order, on the given timings — and invent nothing that is not
# on it. The ban is on ADDING scenes/people/products, never on the changes the
# screenplay asked for.
_OMNI_SHOT_LIST_CLAUSE = (
    "Follow the shot list exactly and in order, cutting at the given times — "
    "each entry is its own scene, and the scene must genuinely change at each "
    "cut, with a hard cut and no dissolve, wipe or transition effect. Obey each "
    "shot's framing and exclusions exactly: a shot marked INSERT CUTAWAY must "
    "NOT show her face. Keep the same creator, wardrobe and grade throughout. "
    "Do not add any scene, location, person or product that is not on the list."
)

# One continuous take of speech under the cuts. Named explicitly because the
# whole point of cutting in-clip (rather than across clips) is that the line is
# rendered once and rides over the edit.
_OMNI_CONTINUOUS_AUDIO_CLAUSE = (
    "The spoken line runs continuously across every cut as one unbroken take — "
    "it does not restart, pause or repeat at a cut."
)


def _omni_shot_scene(beat: Any, *, labelled: bool = True) -> str:
    """One shot's scene, WORD FOR WORD from the screenplay's own fields.

    Nothing here is invented or generalised: a talking head gets its framing's
    camera language plus its authored visual_note, a cutaway gets its authored
    broll_shot/visual_note plus its b-roll camera.

    Each entry opens with a SHOT-KIND LABEL, and a cutaway states its
    EXCLUSIONS. Both were established by live A/B (2026-08-05,
    scratch-probe/omni-multishot-trial{,2}.mp4): with the scene described but
    nothing excluded, Omni rendered "her hand pressing against her stomach" as
    just another talking-to-lens angle. Naming the label and what must stay OUT
    of frame produced the shot exactly as written. Saying what to show is not
    enough — you have to say what must not appear.
    """
    if beat.shot_type == "talking_head":
        cam = _FRAMING_MOTION_CAMERA.get(getattr(beat, "framing", None) or "")
        staging = (f"{cam}, the creator talking straight to the lens" if cam else
                   "the creator holds the phone at arm's length in a handheld "
                   "selfie framing, talking straight to the lens")
        note = (getattr(beat, "visual_note", "") or "").strip()
        body = f"{staging}{f'. {note}' if note else ''}"
        return f"ON CAMERA, SPEAKING: {body}" if labelled else body
    shot = (getattr(beat, "broll_shot", None)
            or getattr(beat, "visual_note", "") or "").strip()
    body = (
        f"{shot}. {_omni_broll_camera(beat)}. Her face, mouth and head are OUT "
        f"OF FRAME entirely for this whole shot — do not show her face, and do "
        f"not show anyone talking"
    )
    return (f"INSERT CUTAWAY, NOBODY SPEAKING ON CAMERA: {body}" if labelled
            else body)


def _omni_groupable(beat: Any, *, uses_product: bool = False) -> bool:
    """May this beat share a generation with its neighbours?

    Grouping is what lets the dialogue run continuously over a scene change, so
    a beat is only held back when it needs a reference image of its OWN that a
    shared generation cannot carry:

      - ``uses_product``: the beat's anchor is rendered WITH the real product,
        grounded on a real surface, and a generation carries one reference image.
        A product shot inside someone else's generation has no product to
        reference, and an invented one is the exact failure the product-grounding
        rule exists to prevent. Its screenplay scene is untouched either way —
        it simply gets its own clip.
      - the hook: the first seconds decide whether the ad is watched, so it stays
        alone on its own rendered frame.
      - silent beats: nothing is being said, so there is no continuous line for
        the cut to ride over — the reason to group is absent.
    """
    if uses_product or (getattr(beat, "ad_section", "") == "hook"):
        return False
    return resolve_voice_mode(beat) != "silent"


def _omni_shot_plan(beat: Any, window_len: float = 0.0) -> list[dict]:
    """The take's shots as data: [{start, end, label, scene}], or [] if not a take.

    This is what Opus is HANDED so it can write the timecode shot list without
    inventing the scenes, their order or their cut times — the screenplay owns
    all three. The same data backs the deterministic builder, so the Opus path
    and the fallback path can never describe different shots.

    ``label`` carries the shot kind because naming it (and, for a cutaway, what
    must stay out of frame) is what makes Omni actually render an insert rather
    than another talking angle — established by live A/B, see
    scratch-probe/omni_multishot_trial2.py.
    """
    shots = list(getattr(beat, "shots", None) or [])
    if len(shots) < 2:
        return []
    lens = list(getattr(beat, "shot_lens", None) or [])
    if len(lens) != len(shots):
        lens = [window_len / len(shots)] * len(shots)
    plan: list[dict] = []
    cursor = 0.0
    prev_end = 0
    total = sum(lens)
    for j, (b, ln) in enumerate(zip(shots, lens)):
        cursor += ln
        start = prev_end
        end = round(total) if j == len(shots) - 1 else round(cursor)
        if end <= start:
            end = start + 1
        plan.append({
            "start": start,
            "end": end,
            "label": ("ON CAMERA, SPEAKING" if b.shot_type == "talking_head"
                      else "INSERT CUTAWAY, NOBODY SPEAKING ON CAMERA"),
            "scene": _omni_shot_scene(b, labelled=False).rstrip(". "),
        })
        prev_end = end
    return plan


def _omni_shot_list(shots: list, lens: list[float]) -> str:
    """Timecode-block shot list — one block per beat, in screenplay order.

    Boundaries are whole seconds, the granularity Omni's own documented examples
    use ("[0-3s] … [3-6s] …"), and the final block closes on the take's end so no
    segment is left open. Blocks are built to be monotonic even when rounding
    would otherwise collapse a short beat to zero length.
    """
    lines: list[str] = []
    cursor = 0.0
    prev_end = 0
    total = sum(lens)
    for j, (b, ln) in enumerate(zip(shots, lens)):
        cursor += ln
        start = prev_end
        end = round(total) if j == len(shots) - 1 else round(cursor)
        if end <= start:
            end = start + 1
        # Every block is a DIFFERENT screenplay beat — _omni_group_shots refuses
        # to fold two shots of the same beat, so a block never has to describe
        # "the same scene again" and the shot-list clause can demand a real
        # change at every cut without contradicting the list.
        # rstrip the period: the screenplay's visual_note usually ends in one,
        # and "camera locked.." reads as a typo to a model reading prose.
        lines.append(f"[{start}-{end}s] {_omni_shot_scene(b).rstrip('. ')}.")
        prev_end = end
    return "\n".join(lines)


_OMNI_BRISK_DELIVERY_CLAUSE = (
    "Speak at an upbeat conversational clip — energetic and moving, starting "
    "immediately with no pause before the first word and none after the last, "
    "but never rushed, clipped or auctioneer-fast."
)

# Physics/material language is Omni's realism lever (the counterpart of the
# Seedance texture tail), and the meta-directive raises the whole quality bar.
_OMNI_REALISM_CLAUSE = (
    "Soft diffused light with steady exposure, natural head movement, subtle "
    "micro-expressions, a realistic blink rate, and fabric that moves with "
    "natural draping rather than stiffly."
)
_OMNI_META_CLAUSE = (
    "Consider micro-detail, expression and timing to create a very rich, "
    "detailed but entirely natural scene."
)


def _omni_broll_camera(beat: Any) -> str:
    """ONE camera instruction for an Omni b-roll clip.

    Omni treats camera vocabulary as commands and even stillness must be
    stated, so there is no "say nothing" option here. Product/hands shots take
    the same gentle push-in the i2v path uses (a locked frame over a static
    product reads as a frozen clip).
    """
    if getattr(beat, "broll_type", None) in _PRODUCT_HANDS_BROLL_TYPES:
        return ("The camera makes one quick, smooth, confident push-in — a "
                "clean scale only, no orbit, pan or parallax")
    return ("Handheld smartphone camera with a natural sway, one "
            "purposeful move only, no jitter")


def _omni_talking_prompt(beat: Any, persona_clause: str = "",
                         pronunciations: Any = None, *,
                         window_len: float = 0.0) -> str:
    """Omni talking head: the model renders the creator SAYING the line.

    Fallback for (and baseline example handed to) the Opus writer. Everything
    the run's voice identity depends on is here verbatim — the persona clause
    is byte-identical across every clip of the run, which together with the
    run-level seed is the whole cross-clip voice-consistency bet.
    """
    section = getattr(beat, "ad_section", "solution") or "solution"
    tone = _SECTION_MOTION.get(section, _SECTION_MOTION["solution"])
    micro = _MICRO_GESTURES[(getattr(beat, "index", 0) or 0) % len(_MICRO_GESTURES)]
    line = _native_spoken_line(beat, pronunciations)
    voice_dir = _native_voice_direction(beat)
    # The camera follows the beat's FRAMING (docs/framing-taxonomy.md) — this
    # builder used to hardcode the arm's-length selfie for every framing, so
    # Omni-mode variety died at the fallback (2026-08-01).
    cam = _FRAMING_MOTION_CAMERA.get(getattr(beat, "framing", None) or "")
    staging = (
        f"{cam}, the creator talking straight to the lens"
        if cam else
        "the creator holds the phone at arm's length in a handheld selfie "
        "framing, talking straight to the lens"
    )
    return (
        f"Create a vertical 9:16 UGC social video. {_OMNI_CHAR_REF_CLAUSE}\n\n"
        f"{_OMNI_SINGLE_SHOT_CLAUSE}: {staging}, "
        f"{tone}, with {micro}. Natural smartphone capture. "
        f"Real-time pace for the whole "
        f"{max(window_len, 1.0):.0f}-second take, never slow motion.\n\n"
        f"Audio: {voice_dir}, the creator says out loud: \"{line}\". "
        f"{persona_clause} {_OMNI_LINE_FIDELITY_CLAUSE} "
        f"{_OMNI_BRISK_DELIVERY_CLAUSE} "
        f"{_OMNI_NO_MUSIC_CLAUSE}\n\n"
        f"{_SKIN_TEXTURE_CLAUSE} {_OMNI_REALISM_CLAUSE} {_OMNI_META_CLAUSE}\n\n"
        f"{_ANATOMY_CLAUSE} {_OMNI_NO_TEXT_CLAUSE}"
    )


def _omni_vo_broll_prompt(beat: Any, persona_clause: str = "",
                          pronunciations: Any = None, *,
                          uses_product: bool = False,
                          window_len: float = 0.0) -> str:
    """Omni cutaway that still carries the spoken line as an off-camera VO."""
    line = _native_spoken_line(beat, pronunciations)
    shot = (getattr(beat, "broll_shot", None) or beat.visual_note or "").strip()
    return (
        f"Create a vertical 9:16 UGC social video. {_OMNI_SCENE_REF_CLAUSE}"
        + (f" {_OMNI_PRODUCT_REF_CLAUSE}" if uses_product else "")
        + f"\n\n{_OMNI_SINGLE_SHOT_CLAUSE}: {shot}. "
        f"{_omni_broll_camera(beat)}. Real-time pace for the whole "
        f"{max(window_len, 1.0):.0f}-second take, never slow motion.\n\n"
        # Double quotes, like the talking builder: an apostrophe inside the line
        # ("it's", "don't") would close a single-quoted span early and the model
        # would read the remainder as prose direction instead of speech.
        f"Audio: the same voice continues off-camera as a voiceover, saying: "
        f"\"{line}\". {persona_clause} {_OMNI_LINE_FIDELITY_CLAUSE} "
        f"{_OMNI_BRISK_DELIVERY_CLAUSE} {_OMNI_NO_MUSIC_CLAUSE}\n\n"
        + (f"{_PRODUCT_STABILITY_CLAUSE} " if uses_product else "")
        + f"{_OMNI_REALISM_CLAUSE} {_OMNI_META_CLAUSE}\n\n"
        f"{_ANATOMY_CLAUSE} {_OMNI_NO_TEXT_CLAUSE}"
    )


def _omni_multishot_prompt(beat: Any, persona_clause: str = "",
                           pronunciations: Any = None, *,
                           window_len: float = 0.0) -> str:
    """One Omni generation carrying SEVERAL screenplay beats as hard cuts.

    The dialogue is rendered once and runs straight through the cuts, which is
    the whole reason to group: no seam at a clip join, nothing to truncate at a
    boundary, and no per-clip duration floor to over-buy. Each beat contributes
    one timecode block with its own scene, exactly as the screenplay wrote it.

    The grouped beat carries its parts on ``shots``/``shot_lens`` (stamped by
    _omni_windows). Its own ``dialogue_text`` is the parts' lines joined in
    order, so the line fidelity clause, the caption force-align and the verbatim
    checks all still see the complete spoken text.
    """
    shots = list(getattr(beat, "shots", None) or [beat])
    lens = list(getattr(beat, "shot_lens", None) or [window_len])
    line = _native_spoken_line(beat, pronunciations)
    voice_dir = _native_voice_direction(shots[0])
    # WHERE the voice comes from, shot by shot. This is the clause that actually
    # made a cutaway render as a cutaway (trial 1 → 2, 2026-08-05): saying "the
    # creator says out loud" for the WHOLE take tells the model she is on camera
    # talking the entire time, so it quietly turned the insert back into a
    # talking shot. Naming the on-camera shots and sending the voice off-camera
    # for the rest is what fixed it.
    on_cam = [str(j + 1) for j, s in enumerate(shots)
              if s.shot_type == "talking_head"]
    if on_cam and len(on_cam) < len(shots):
        placement = (
            f"She is ON CAMERA speaking it during "
            f"shot{'s' if len(on_cam) > 1 else ''} {', '.join(on_cam)}; during "
            f"every INSERT CUTAWAY shot her voice CONTINUES as an off-camera "
            f"voiceover over the cutaway while she is not visible. "
        )
    elif not on_cam:
        placement = ("Her voice is an off-camera voiceover for the whole take — "
                     "she is never visible speaking. ")
    else:
        placement = ""
    return (
        f"Create a vertical 9:16 UGC social video. {_OMNI_CHAR_REF_CLAUSE}\n\n"
        f"{len(shots)} shots in one continuous "
        f"{max(window_len, 1.0):.0f}-second take:\n"
        f"{_omni_shot_list(shots, lens)}\n"
        f"Natural smartphone capture, real-time pace, never slow motion. "
        f"{_OMNI_SHOT_LIST_CLAUSE}\n\n"
        f"Audio: {voice_dir}, one unbroken take of speech across all "
        f"{len(shots)} shots that never restarts, pauses or repeats: "
        f"\"{line}\". {placement}"
        f"{persona_clause} {_OMNI_LINE_FIDELITY_CLAUSE} "
        f"{_OMNI_BRISK_DELIVERY_CLAUSE} {_OMNI_NO_MUSIC_CLAUSE}\n\n"
        f"{_SKIN_TEXTURE_CLAUSE} {_OMNI_REALISM_CLAUSE} {_OMNI_META_CLAUSE}\n\n"
        f"{_ANATOMY_CLAUSE} {_OMNI_NO_TEXT_CLAUSE}"
    )


def _omni_silent_broll_prompt(beat: Any, *, uses_product: bool = False,
                              window_len: float = 0.0) -> str:
    """Omni cutaway / reaction hold with no dialogue at all.

    The ambient sound must be NAMED: given no audio direction Omni scores the
    clip, and a music bed under one beat of an otherwise dry ad is a re-roll.
    """
    shot = (getattr(beat, "broll_shot", None) or beat.visual_note or "").strip()
    return (
        f"Create a vertical 9:16 UGC social video. {_OMNI_SCENE_REF_CLAUSE}"
        + (f" {_OMNI_PRODUCT_REF_CLAUSE}" if uses_product else "")
        + f"\n\n{_OMNI_SINGLE_SHOT_CLAUSE}: {shot}. "
        f"{_omni_broll_camera(beat)}. Real-time pace for the whole "
        f"{max(window_len, 1.0):.0f}-second take, never slow motion.\n\n"
        f"Audio: only the natural ambient sound of the scene — soft room tone "
        f"and the quiet handling noise of the action itself. No dialogue, no "
        f"music.\n\n"
        + (f"{_PRODUCT_STABILITY_CLAUSE} " if uses_product else "")
        + f"{_OMNI_REALISM_CLAUSE} {_OMNI_META_CLAUSE}\n\n"
        f"{_ANATOMY_CLAUSE} {_OMNI_NO_TEXT_CLAUSE}"
    )


def _omni_safety_tail(prompt: str, *, mode: str,
                      persona_clause: str = "",
                      uses_product: bool = False) -> str:
    """Re-append Omni's safety-critical clauses if the Opus rewrite lost them.

    Same rule (and same reason) as the anatomy re-append: a paraphrased ban is a
    silently removed guard. It bites harder here because Omni has NO
    negative_prompt — every constraint is positive text in this one string, and
    each clause below guards a documented default failure (self-directed scene
    cuts; an invented music bed under the dialogue; a rendered lower-third that
    our own burned captions would then double). Bans are checked affirmatively on
    their own core words rather than by diffing against the builder output, so a
    legitimately reworded brief that still carries the guard is left alone.

    The voice persona is the one clause checked VERBATIM: it is not a ban but an
    identity, and only byte-identical wording across the run's clips holds one
    voice across the cuts — a "close enough" paraphrase is exactly the failure.
    """
    low = prompt.lower()
    # A folded take MUST cut, so the single-take lock is the one guard that must
    # NOT be re-appended here — doing so would staple "no scene cuts" onto the
    # end of a timecode shot list and undo the whole multi-shot instruction. Its
    # counterpart discipline (hard cuts, nothing added, one grade) is guarded
    # instead.
    if mode == "omni_multishot":
        if "dissolve" not in low:
            prompt = f"{prompt.rstrip()} {_OMNI_SHOT_LIST_CLAUSE}"
    elif "no scene cuts" not in low:
        prompt = f"{prompt.rstrip()} {_OMNI_SINGLE_SHOT_CLAUSE}."
    if (mode in ("omni_talk", "omni_vo_broll", "omni_multishot")
            and "background music" not in low):
        prompt = f"{prompt.rstrip()} {_OMNI_NO_MUSIC_CLAUSE}"
    if "on-screen text" not in low:
        prompt = f"{prompt.rstrip()} {_OMNI_NO_TEXT_CLAUSE}"
    if (mode in ("omni_talk", "omni_vo_broll", "omni_multishot")
            and persona_clause
            and _omni_norm(persona_clause) not in _omni_norm(prompt)):
        prompt = f"{prompt.rstrip()} {persona_clause}"
    # Reference clauses are the IDENTITY anchors — the anchor image only holds
    # face/scene/product if the prompt DECLARES what the image is (measured:
    # silence about it drifts the wardrobe). Opus dropping them was unguarded
    # until 2026-08-01. Checked on core words like the bans above.
    low = prompt.lower()
    # A take's anchor is its FIRST shot's frame, which may be a character frame
    # or a scene frame, so either declaration counts for it.
    if (mode == "omni_talk" and "character reference" not in low) or (
            mode == "omni_multishot" and "character reference" not in low
            and "opening frame" not in low):
        prompt = f"{prompt.rstrip()} {_OMNI_CHAR_REF_CLAUSE}"
    if (mode in ("omni_vo_broll", "omni_silent")
            and "opening frame" not in low):
        prompt = f"{prompt.rstrip()} {_OMNI_SCENE_REF_CLAUSE}"
    if uses_product and "product reference" not in low:
        prompt = f"{prompt.rstrip()} {_OMNI_PRODUCT_REF_CLAUSE}"
    return prompt


def _omni_norm(s: str) -> str:
    """Whitespace/case/tag-insensitive form for containment checks on prompts."""
    return re.sub(r"\s+", " ", _strip_tags(s)).strip().casefold()


def _omni_line_survived(prompt: str, beat: Any,
                        pronunciations: Any = None) -> bool:
    """Did this beat's spoken line survive into the final Omni prompt?

    Omni speaks ONLY what the prompt quotes, and the audio IS the render — a
    clip whose line was dropped or paraphrased cannot be repaired in assembly,
    it can only be re-paid. Compared on a normalized form (tags stripped,
    whitespace collapsed, case-folded) against BOTH spellings the writer could
    legitimately have used: the raw planner line it was handed, and the
    respelled/de-CAPSed form the deterministic builder quotes.
    """
    spoken = _strip_tags(beat.dialogue_text or "").strip()
    if not spoken:
        return True
    hay = _omni_norm(prompt)
    return any(_omni_norm(cand) in hay for cand in
               (spoken, _native_spoken_line(beat, pronunciations)))


def _broll_motion_prompt(broll_shot: str, visual_note: str,
                         broll_type: str | None) -> str:
    # Positive, concise, camera separated from subject (per the skill file).
    # ONE camera instruction; the product stays rigid. Product/hands shots take
    # a SLOW controlled push-in (camera_fixed=False) instead of a hard lock — a
    # locked camera over their static subjects froze the clip and sometimes
    # spawned a phantom hand. The push-in is a gentle scale only (no orbit/pan/
    # parallax), so _PRODUCT_STABILITY_CLAUSE still prevents label/shape morph.
    # Lifestyle / no-product b-roll keeps its gentle handheld phone feel.
    if broll_type in _PRODUCT_HANDS_BROLL_TYPES:
        camera = _PRODUCT_PUSHIN_CAMERA
        # Product/hands hero: the product stays rigid while the camera pushes
        # in with intent. Grain dirties a clean label, so this path uses
        # material realism, NOT the film-grain tail. product_in_scene RESTS (no
        # hand enters a hand-free frame); held/hands types keep the hold rules.
        action = (_PRODUCT_REST_CLAUSE if broll_type == "product_in_scene"
                  else _PRODUCT_HOLD_CLAUSE)
        body = (f"{_PRODUCT_STABILITY_CLAUSE} {action} "
                f"{_SINGLE_PRODUCT_CLAUSE} {_PRODUCT_TEXTURE_CLAUSE}")
        # Reference-first: faithfulness is FRONT-LOADED (Seedance down-weights
        # the tail) and visual_note is dropped — it re-describes a product the
        # planner never saw, and that text is what the animation drifts toward
        # (the standalone-product hallucination, verified 2026-07-31). The
        # anchor image already encodes the staging.
        return (
            f"{_FAITHFULNESS_CLAUSE} {broll_shot}. "
            f"{camera}. {body} {_ANATOMY_CLAUSE}"
        )
    camera = "Natural lively handheld camera movement at real-time pace, " \
             "phone-camera feel"
    # Lifestyle / no-product: the full grain tail is where "real phone
    # footage" sells hardest, and there's no label to keep pristine. The
    # product-stability ban is dropped here — there's no product to hold.
    body = f"{_SETTLE_CLAUSE} {_SKIN_TEXTURE_CLAUSE}"
    return (
        f"{broll_shot}. {visual_note}. "
        f"{camera}. {body} {_FAITHFULNESS_CLAUSE} {_ANATOMY_CLAUSE}"
    )


# ---- Kling v3 b-roll prompts ---------------------------------------------
# Kling is prompted NOTHING like Seedance, so it gets its own builder rather
# than a flag on _broll_motion_prompt:
#   * Kling's official i2v formula is "Subject + Movement, Background +
#     Movement" — MOTION only. Its documented failure mode is that "a
#     description that significantly deviates from the image may cause a camera
#     cut or transition", and _broll_motion_prompt's 180-250 words of
#     re-described product (shape, label, printed text) IS that deviation.
#   * Kling HAS negative_prompt. Every affirmative ban clause we carry only
#     because Seedance lacks the field moves there as bare noun phrases.
#   * Kling docs: "use simple words and sentence structures"; fal's guidance is
#     50-150 words. We target 400 chars (hard API cap is 2500).
_KLING_PROMPT_CHARS = 400

# Kling i2v has NO camera_fixed / camera_control, so the ONLY camera lock is to
# say nothing about the camera in the positive and ban the moves in
# negative_prompt (both lists below carry "camera shake, zoom, pan"). A camera
# sentence in the positive would fight its own negative, so the repair drops it.
# NOT "lens": in this codebase "turns it to the lens" / "looks at the lens" is
# SUBJECT direction, not a camera move — matching it dropped the only real
# motion out of a product beat.
_KLING_CAMERA_RE = re.compile(
    r"\b(camera|cameras|filmed|shot on|push-?in|pull-?back|zooms?|zooming|"
    r"pans?|panning|orbits?|dolly|dollies|tilts?|crane|tracking|parallax|"
    r"handheld)\b",
    re.I,
)

# Vocabulary that exists ONLY to work around Seedance's missing negative_prompt
# (bans, object counts, anti-warp guards). On Kling it belongs in
# negative_prompt: left in the positive it burns the char budget AND re-describes
# the product, which is exactly what triggers Kling's camera-cut failure mode.
_KLING_BAN_RE = re.compile(
    r"\b(never|not|no|nothing|none|avoid|without|phantom|warps?|warped|"
    r"warping|morphs?|morphing|duplicate|distorts?|distorted|distortion)\b",
    re.I,
)

# Hold-still vocabulary — _SETTLE_CLAUSE and any paraphrase of it. On Seedance
# that clause is protective (the clip is trimmed to a fractional length, so a
# still tail means the cut lands on near-frozen footage); on Kling it is the
# documented recipe for a DEAD clip, and it directly contradicts the
# micro-motion the repair appends ("hold a static framing" + "keep moving" in
# one prompt). The reaction path already swaps it for _KLING_REACTION_MOTION,
# but the b-roll path could not: the clause is affirmative, so it carries no
# _KLING_BAN_RE word, and "the shot settles" carries no _KLING_CAMERA_RE word
# either — it sailed through the repair untouched. A regex rather than a
# substring match on _SETTLE_CLAUSE (the way _FAITHFULNESS_CLAUSE is matched)
# because the clause reaches Kling via OPUS: _write_motion_prompts hands Opus
# fallback_example=_broll_motion_prompt(...), which always contains it for
# lifestyle b-roll, and Opus's paraphrase is what becomes motion_source.
_KLING_FREEZE_RE = re.compile(
    r"\bsettl(?:e|es|ed|ing)\b|"
    r"\b(?:held|static|frozen|locked|motionless|still)\s+"
    r"(?:framing|frame|shot|composition|pose)\b|"
    r"\bfor the (?:final|last) second\b|"
    r"\bstops? moving\b|\bcomes? to a (?:stop|rest|halt)\b|"
    r"\bholds? (?:completely )?(?:still|steady) (?:for|through|until)\b",
    re.I,
)

# Kling warns that a vague prompt can come back FROZEN, and with the camera
# locked the SUBJECT is the only motion source — so every Kling beat names ONE
# concrete micro-motion. Deliberately phrased with no camera word and no ban
# word, so the repair below can never strip a beat's only motion.
_KLING_MICRO_MOTION: dict[str, str] = {
    "product_closeup": "the product stays put while the light shifts gently "
                       "across its label",
    "product_in_scene": "the product keeps resting where it sits while the "
                        "light shifts gently across it",
    "hands_using": "the hands keep their grip and complete one quick, "
                   "precise movement",
    "demo_steps": "the hands complete one quick, precise step of the routine",
    "texture_pour": "the texture flows in one smooth, continuous pour",
    "unboxing": "the hands lift the lid in one swift, continuous movement",
    "before_after": "the hands hold steady and turn the product a few degrees "
                    "toward the lens",
}
_KLING_MICRO_MOTION_DEFAULT = (
    "she continues the action naturally with small, relaxed body movement and "
    "one natural blink"
)
# Reaction cutaways: _SETTLE_CLAUSE ("settles into a calm, steady, held framing
# for the final second") is a literal instruction to stop moving — on Kling that
# is the documented recipe for a dead clip, so the reaction path names a
# micro-motion instead of settling.
_KLING_REACTION_MOTION = (
    "she reacts naturally with a small shift of expression, one natural blink "
    "and easing shoulders, moving gently and continuously"
)

# Product/hands b-roll bans: label + geometry stability, the phantom-hand and
# duplicate-product hallucinations, the cap-opening/spraying one, plus the
# camera-lock and generic-quality terms. Bare noun phrases — Kling's
# negative_prompt takes the artifact NAMES, never "no X" phrasing.
_KLING_NEG_PRODUCT = (
    "morphing product, warping label, distorted text, floating text, peeling "
    "label, product changing size, product changing shape, duplicate product, "
    "new object entering frame, cap opening, spraying, extra hand, disembodied "
    "hand, extra fingers, warped hands, scene cut, transition, camera shake, "
    "zoom, pan, blur, distortion, low quality, text overlay, watermark, cartoon"
)
# Person-in-frame b-roll (lifestyle) AND reaction cutaways. Skin negatives stay
# FLATTERING: they ban the plastic/waxy CGI sheen and blotchiness, and never ask
# for pore/oil emphasis that would make an attractive creator read greasy (same
# rule as _SKIN_TEXTURE_CLAUSE). "frozen, static" is here because a locked
# camera + a person is where Kling's dead-clip failure mode bites hardest.
_KLING_NEG_PERSON = (
    "distorted face, changing face, extra fingers, warped hands, extra limbs, "
    "plastic skin, waxy skin, oily skin, blotchy skin, scene cut, transition, "
    "camera shake, blur, distortion, low quality, text overlay, watermark, "
    "cartoon, slow motion, frozen, static"
)


def _kling_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip())
            if s.strip()]


def _kling_fit(sentences: list[str], budget: int) -> str:
    """Longest whole-sentence prefix of `sentences` fitting in `budget` chars.

    Truncating mid-sentence would leave Kling a dangling clause to interpret,
    so the cap lands on a sentence boundary. A single sentence longer than the
    whole budget (an Opus run-on) is hard-cut on the last word boundary and
    closed, so the prompt never ends mid-word.

    A non-positive budget means the reserved micro-motion already fills the cap,
    so there is room for nothing else. Guarded explicitly because `budget` ends
    up as a slice index below, and `s[:-10]` silently returns a LONG fragment
    (everything but the last 10 chars) instead of the empty string.
    """
    if budget <= 0:
        return ""
    out: list[str] = []
    used = 0
    for s in sentences:
        add = len(s) + (1 if out else 0)
        if used + add > budget:
            break
        out.append(s)
        used += add
    if out:
        return " ".join(out)
    if not sentences:
        return ""
    head = sentences[0][:budget].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return f"{head}." if head else ""


def _kling_keep(candidate: str, *, lock_camera: bool = True) -> list[str]:
    """The sentences of `candidate` that belong in a Kling positive.

    Dropped: Seedance-style ban / anti-warp guards (they move to
    negative_prompt), hold-still directions (Kling's dead-clip recipe — see
    _KLING_FREEZE_RE), any faithfulness tail the source already carried (the
    repair re-appends exactly one), and — when the shot is locked — camera
    directions, since Kling has no camera_fixed and omission IS the lock.
    """
    kept: list[str] = []
    for s in _kling_sentences(candidate):
        if (_FAITHFULNESS_CLAUSE in s or _KLING_BAN_RE.search(s)
                or _KLING_FREEZE_RE.search(s)):
            continue
        if lock_camera and _KLING_CAMERA_RE.search(s):
            continue
        kept.append(s)
    return kept


def _kling_positive(candidate: str, *, micro_motion: str = "",
                    lock_camera: bool = True) -> str:
    """Deterministic validator/repair: any motion text → a Kling-shaped positive.

    Pure and independently testable — computable failures are fixed in CODE, not
    by re-prompting (project rule). Three repairs, in order:
      1. keep only the Kling-appropriate sentences (see _kling_keep);
      2. fit to _KLING_PROMPT_CHARS on a sentence boundary, with `micro_motion`
         RESERVED out of the budget so the one named movement can never be the
         sentence the cap drops (a Kling clip with no stated motion comes back
         frozen);
      3. append the faithfulness tail EXACTLY once.
    Returns "" only when there is nothing at all to say — a tail-only prompt is
    never shipped.
    """
    kept = _kling_keep(candidate, lock_camera=lock_camera)
    micro = micro_motion.strip()
    budget = _KLING_PROMPT_CHARS - len(_FAITHFULNESS_CLAUSE) - 1
    if micro:
        budget -= len(micro) + 1
    parts = [p for p in (_kling_fit(kept, budget), micro) if p]
    return f"{' '.join(parts)} {_FAITHFULNESS_CLAUSE}" if parts else ""


def _kling_micro_motion(beat: Any, *, mode: str) -> str:
    """The ONE named movement a Kling beat is guaranteed to state, as a sentence.

    Kling's documented vague-prompt failure is a frozen clip and the camera is
    locked, so the subject must be given something concrete to do. Chosen by
    broll_type; reaction cutaways get the reaction motion (which is what replaces
    _SETTLE_CLAUSE — see _KLING_REACTION_MOTION).
    """
    phrase = (
        _KLING_REACTION_MOTION if mode == "reaction"
        else _KLING_MICRO_MOTION.get(getattr(beat, "broll_type", None) or "",
                                     _KLING_MICRO_MOTION_DEFAULT)
    )
    return f"{phrase[0].upper()}{phrase[1:]}."


def _kling_subject(beat: Any, *, mode: str) -> str:
    """Deterministic subject clause: the planner's shot line, per Kling's
    "Subject + Movement" formula. visual_note is deliberately absent — it
    re-describes staging the anchor image already encodes, and re-description is
    exactly what makes Kling cut. Reaction beats have no shot line and need none:
    the anchor frame is the subject, the micro-motion is the movement.
    """
    if mode == "reaction":
        return ""
    shot = (getattr(beat, "broll_shot", None) or "").strip().rstrip(".")
    return f"{shot[0].upper()}{shot[1:]}." if shot else ""


def _kling_negative(beat: Any, *, mode: str) -> str:
    """Product-stability bans for product/hands b-roll; anatomy/skin bans for
    person-in-frame shots (lifestyle b-roll + reaction cutaways)."""
    broll_type = getattr(beat, "broll_type", None)
    if mode != "reaction" and (broll_type in _PRODUCT_HANDS_BROLL_TYPES
                               or broll_type in _HANDS_BROLL_TYPES):
        return _KLING_NEG_PRODUCT
    return _KLING_NEG_PERSON


def _kling_broll_prompt(beat: Any, *, mode: str,
                        motion_source: str | None = None) -> tuple[str, str]:
    """(positive, negative) for one Kling b-roll/reaction beat.

    `motion_source` is the Opus-written motion text for the beat (the same LLM
    seam the Seedance b-roll prompt uses, grounded in the beat's anchor frame);
    the deterministic repair is what makes it Kling-shaped. Without Opus — or
    when the repair strips the source down to nothing — the planner's shot line
    is the candidate instead, so a paid run never ships an empty or
    camera-fighting prompt. Either way the named micro-motion closes the
    positive, so the clip always has one stated movement.
    """
    candidate = motion_source or ""
    if not _kling_keep(candidate):
        candidate = _kling_subject(beat, mode=mode)
    positive = _kling_positive(
        candidate, micro_motion=_kling_micro_motion(beat, mode=mode))
    return positive, _kling_negative(beat, mode=mode)


def resolve_voice_mode(beat) -> str:
    """Effective voice_mode for a beat (planner value wins; else derived).

    on_camera     = talking_head speech (lip-synced native voice)
    background_vo = broll/off-screen shot that still carries a spoken line
    silent        = reaction hold OR broll with no dialogue (ambient only)
    """
    explicit = getattr(beat, "voice_mode", None)
    if explicit in ("on_camera", "background_vo", "silent"):
        return explicit
    if getattr(beat, "beat_kind", "speech") == "reaction":
        return "silent"
    has_line = bool((getattr(beat, "dialogue_text", "") or "").strip())
    if beat.shot_type == "talking_head":
        return "on_camera" if has_line else "silent"
    return "background_vo" if has_line else "silent"


def _beat_mode(beat: Any, *, lipsync_engine: str,
               isolated_audio: bool = False,
               omni_mode: bool = False) -> str:
    """Classify a beat for prompt generation — mirrors _clip's branch logic.

    omni: every beat routes onto an omni_* mode (omni_talk / omni_vo_broll /
    omni_silent) by the planner's voice intent — those are the only prompt
    families Omni has, and they key both the Opus task template and the
    fallback builder. Reaction beats are NOT split out as their own mode here:
    Omni authors each clip's audio, so a hold still needs an explicit
    ambient-only brief (which is exactly omni_silent).

    seedance_native engine: delegates to resolve_voice_mode so every branch
    (on_camera / background_vo / silent) is driven by the planner's intent
    rather than shot_type alone.

    Isolated-audio engines (heygen_replicate always; syncso in per_clip
    granularity via ``isolated_audio=True``): also delegate to
    resolve_voice_mode (on_camera → talking-head lipsynced against its
    isolated per-beat audio; background_vo → b-roll with per-beat audio
    overlay; silent → ambient-only). Prompt routing keeps silent i2v:
    on_camera beats get _talking_motion_prompt (the mouth-moving clause sync
    needs) and background_vo/silent get _broll_motion_prompt — see
    _fallback_prompt.

    Legacy engines (direct heygen; syncso "whole"): reaction → silent hold
    (skips Opus); native_talk → dead branch kept for safety; sync_talk →
    talking head lipsynced downstream; broll → cutaway.
    """
    if omni_mode:
        return {
            "on_camera": "omni_talk",
            "background_vo": "omni_vo_broll",
            "silent": "omni_silent",
        }[resolve_voice_mode(beat)]
    is_reaction = getattr(beat, "beat_kind", "speech") == "reaction"
    if is_reaction:
        return "reaction"
    if (lipsync_engine in ("seedance_native", "heygen_replicate")
            or isolated_audio):
        return resolve_voice_mode(beat)
    if beat.shot_type == "talking_head":
        return "sync_talk"
    return "broll"


def _clip_audio_url(mode: str, anchor_url: str | None) -> str | None:
    """Return the shared timbre anchor URL for speaking modes; None for silent.

    on_camera    → anchor_url  (talking head: voice baked in via r2v)
    background_vo → anchor_url (off-screen VO: audio rides the clip)
    silent        → None       (reaction hold or pure b-roll: no audio ref)

    This is the single place that decides which clips get the timbre anchor
    so the sharing contract is testable without running the full pipeline.
    """
    return anchor_url if mode in ("on_camera", "background_vo") else None


def _use_kling(mode: str, *, native: bool, broll_provider: str) -> bool:
    """Kling handles beats that are neither lip-synced nor audio-driven.

    The `native` guard is load-bearing, not defensive: under
    lipsync_engine="seedance_native" a background_vo beat is generated WITH
    audio on the r2v model (generate_audio=True, the shared timbre anchor rides
    as reference audio). Kling has NO audio-driven generation at all, so those
    beats must stay on Seedance even though they are visually plain b-roll —
    routing them to Kling would silently drop the voiceover.

    Talking beats (sync_talk / on_camera / native_talk) never route here: their
    clip has to carry either baked-in native speech or a mouth that post-hoc
    lipsync can drive, and Seedance owns both paths.

    Omni runs never reach a True here either: their modes are all omni_*-
    prefixed, so no name below can match. The caller ALSO short-circuits on
    omni_mode — a mixed-generator run would send an Omni-style brief to a Kling
    body builder (and split one run's voice across two engines).
    """
    if broll_provider != "kling" or native:
        return False
    return mode in ("broll", "reaction", "silent", "background_vo")


def _fallback_prompt(beat: Any, window_len: float, *,
                     lipsync_engine: str, isolated_audio: bool = False,
                     persona_clause: str = "",
                     pronunciations: Any = None,
                     omni_mode: bool = False,
                     uses_product: bool = False) -> str:
    """The deterministic f-string for a beat — Opus's fallback AND the
    baseline example handed to Opus as the bar to beat.

    omni modes (omni_talk / omni_vo_broll / omni_silent) get the brief builders:
    Omni renders the audio too, so the line, the voice persona and every ban
    have to be IN the prompt — there is no reference audio and no negative
    field to carry them.

    seedance_native modes (on_camera / background_vo / silent) each get their
    dedicated native builder so the mandatory timbre+fidelity tokens are never
    paraphrased away by the Opus rewrite path.

    Isolated-audio modes (heygen_replicate always; syncso per_clip via
    ``isolated_audio=True``): on_camera → _talking_motion_prompt (the
    mouth-moving clause post-hoc lipsync needs — NOT the native timbre prompt
    which embeds the dialogue line); background_vo / silent →
    _broll_motion_prompt (silent i2v, audio attached in assembly). The clips
    are always silent i2v (native=False), so the native-specific tokens are
    never appropriate here.

    Legacy engines keep the original sync_talk / broll / reaction routing.
    """
    isolated = isolated_audio or lipsync_engine == "heygen_replicate"
    mode = _beat_mode(beat, lipsync_engine=lipsync_engine,
                      isolated_audio=isolated_audio, omni_mode=omni_mode)
    if mode in ("omni_talk", "omni_vo_broll", "omni_multishot") and len(
            getattr(beat, "shots", None) or []) > 1:
        # A folded beat is several screenplay shots in one take; its own
        # shot_type is just the first part's, so it must not pick the builder.
        return _omni_multishot_prompt(beat, persona_clause, pronunciations,
                                      window_len=window_len)
    if mode == "omni_talk":
        return _omni_talking_prompt(beat, persona_clause, pronunciations,
                                    window_len=window_len)
    if mode == "omni_vo_broll":
        return _omni_vo_broll_prompt(beat, persona_clause, pronunciations,
                                     uses_product=uses_product,
                                     window_len=window_len)
    if mode == "omni_silent":
        return _omni_silent_broll_prompt(beat, uses_product=uses_product,
                                         window_len=window_len)
    if mode == "on_camera":
        if isolated:
            # Silent i2v for post-hoc lipsync: the mouth-moving clause makes
            # the clip suitable for mouth re-rendering; no timbre tokens.
            return _talking_motion_prompt(beat, window_len)
        return _native_talking_prompt(beat, persona_clause,
                                      pronunciations)
    if mode == "background_vo":
        if isolated:
            return _broll_motion_prompt(
                beat.broll_shot or "", beat.visual_note,
                getattr(beat, "broll_type", None),
            )
        return _native_vo_broll_prompt(beat, persona_clause,
                                       pronunciations)
    if mode == "silent":
        if isolated:
            return _broll_motion_prompt(
                beat.broll_shot or "", beat.visual_note,
                getattr(beat, "broll_type", None),
            )
        return _native_silent_broll_prompt(beat)
    if mode == "sync_talk":
        return _talking_motion_prompt(beat, window_len)
    # broll AND reaction both go through the silent broll prompt today
    return _broll_motion_prompt(
        beat.broll_shot or "", beat.visual_note,
        getattr(beat, "broll_type", None),
    )


def _timeline_overview(beats: list) -> str:
    """Compact full-timeline context shared across the per-beat fan-out, so
    Opus keeps motion/energy flowing across cuts instead of writing each beat
    in isolation."""
    lines = [
        "Full beat sequence (for cross-cut continuity — do NOT re-describe "
        "scenes, just be aware of the arc):"
    ]
    for b in beats:
        kind = getattr(b, "beat_kind", "speech")
        line = (b.dialogue_text or "").strip() or "(silent)"
        lines.append(
            f"  beat {b.index} [{b.ad_section}/{b.shot_type}/{kind}]: "
            f"{line[:80]}"
        )
    return "\n".join(lines)


async def _write_motion_prompts(
    *, beats: list, anchors: list, windows: list, gem: Any,
    character: Any, llm: str, product_ref_url: str | None,
    product_name: str | None = None,
    lipsync_engine: str, isolated_audio: bool = False,
    broll_video_provider: str = BROLL_VIDEO_PROVIDER,
    pronunciations: Any = None, omni_mode: bool = False,
    brief_directives: str = "",
    on_progress: Callable[[str], None],
    manifest: Any, on_asset: Callable[[str, Any], None] | None,
) -> dict[int, dict]:
    """Stage 5b: per-beat motion prompts, written by Opus.

    Concurrent. Under the claude provider every non-reaction beat gets an
    Opus prompt grounded in its anchor frame; any failure (or a non-claude
    provider / reaction beat) falls back to the deterministic f-string so a
    paid run never dies on one bad sample. Each record is persisted debug-rich
    to manifest["motion_prompts"] as it lands.

    The prompt SHAPE is provider-specific (Kling wants a short motion-only
    positive plus a real negative_prompt; Seedance wants the long affirmative
    form), so per-beat provider routing is decided HERE — once, via _use_kling —
    and the clip stage reads it back off the record instead of re-deriving it.
    """
    _assert_aligned(beats=beats, anchors=anchors, windows=windows)
    overview = _timeline_overview(beats)
    records: dict[int, dict] = {}

    # Compute persona_clause ONCE per run and reuse the identical string in
    # every beat's prompt: for a model that synthesizes the voice from words,
    # byte-identical wording is the only thing holding one voice across cuts
    # (paraphrase it per beat and each clip gets a different speaker).
    # Omitted for engines whose clips are silent — the clause is irrelevant
    # there. Omni includes PACE: it authors the whole audio track from this
    # clause alone, with no separate cadence direction to inherit.
    if omni_mode:
        persona_clause = render_voice_persona(
            character.voice_persona, include_pace=True)
    elif lipsync_engine == "seedance_native":
        persona_clause = render_voice_persona(character.voice_persona)
    else:
        persona_clause = ""

    async def _one(i: int, beat: Any, anchor: Any,
                   window: tuple[float, float]) -> None:
        window_len = window[1] - window[0]
        uses_product = _beat_uses_product(beat, product_ref_url, product_name)
        mode = _beat_mode(beat, lipsync_engine=lipsync_engine,
                          isolated_audio=isolated_audio, omni_mode=omni_mode)
        fallback = _fallback_prompt(beat, window_len,
                                    lipsync_engine=lipsync_engine,
                                    isolated_audio=isolated_audio,
                                    persona_clause=persona_clause,
                                    pronunciations=pronunciations,
                                    omni_mode=omni_mode,
                                    uses_product=uses_product)
        prompt, restaged, model = fallback, False, None
        # native_timbre: only the seedance_native engine embeds mandatory
        # timbre+fidelity tokens that must not be paraphrased by Opus.
        # heygen_replicate uses the same mode names (on_camera/background_vo/
        # silent) but its prompts are the normal syncso-style builders — Opus
        # MAY improve them via the regular path below.
        native_timbre_mode = (
            lipsync_engine == "seedance_native"
            and mode in ("on_camera", "background_vo", "silent")
        )
        # A folded multi-shot take: Opus still writes the prompt (it writes them
        # far better than the f-string), but it is HANDED the shot plan — the
        # screenplay's scenes, their order and their cut times — and its output is
        # checked for them. Left to itself it obeys the omni skill doc's
        # single-unbroken-shot lock and flattens the take: on the 2026-08-06 live
        # run both takes came back as one uncut 7s / 9s shot.
        shot_plan = _omni_shot_plan(beat, window_len)
        # Same native flag the clip stage passes to _use_kling: under
        # seedance_native the speaking modes are audio-DRIVEN r2v renders, which
        # Kling cannot do at all — they stay on Seedance and keep their long
        # affirmative prompt.
        # Omni runs never split across providers: one generator authors every
        # clip's video AND voice, so a Kling b-roll beat would both drop the
        # voiceover and break the run's single-voice bet.
        use_kling = not omni_mode and _use_kling(
            mode, broll_provider=broll_video_provider,
            native=(lipsync_engine == "seedance_native"
                    and mode in ("on_camera", "background_vo")),
        )
        if shot_plan:
            # The take's own task template + validation live in write_motion_prompt.
            mode = "omni_multishot"
        if mode == "reaction":
            source = "reaction"
        elif native_timbre_mode:
            # For native-timbre beats the mandatory timbre+fidelity tokens must
            # NOT be paraphrased away by Opus. Use the deterministic f-string as
            # the final prompt — same skip logic as the reaction branch.
            source = "native_timbre"
        elif llm != "claude":
            source = "fallback"
        else:
            try:
                mp = await gem.write_motion_prompt(
                    beat=beat, character=character, window_len=window_len,
                    mode=mode, uses_product=uses_product, shot_plan=shot_plan,
                    anchor_url=anchor.url, timeline_overview=overview,
                    fallback_example=fallback,
                    # Both matter only where the video model authors the AUDIO
                    # (omni): the run-level voice identity must reach the writer
                    # verbatim, and the line it quotes must be the SPOKEN form
                    # (tags stripped, emphasis de-CAPSed, respellings applied) —
                    # whatever the prompt quotes is literally what gets said.
                    # The Seedance task templates interpolate neither, so both
                    # are inert there.
                    persona_clause=persona_clause,
                    spoken_line=_native_spoken_line(beat, pronunciations),
                    brief_directives=brief_directives,
                )
                prompt, restaged, source, model = (
                    mp.prompt, mp.restaged, "opus", CLAUDE_PRO_MODEL)
                # The anatomy guard is safety-critical vocabulary — same rule
                # as the native-timbre tokens: never left to paraphrase. A
                # live beat rendered a warped pointing finger after Opus
                # softened the guard to "five fingers each, gesture fully
                # inside frame" (run v2-20260714-142306, beat 5). If the
                # anti-warp core is missing, append the full clause verbatim.
                # Kling is exempt: it HAS negative_prompt, so the anti-warp
                # guard rides there (see _KLING_NEG_*) instead of bloating a
                # positive prompt that must stay motion-only.
                if not use_kling and "never stretched" not in prompt:
                    prompt = f"{prompt.rstrip()} {_ANATOMY_CLAUSE}"
                if omni_mode:
                    # Omni has no negative_prompt: the single-shot lock and the
                    # music ban only exist as positive text, so they are
                    # re-appended verbatim when the rewrite lost them.
                    prompt = _omni_safety_tail(
                        prompt, mode=mode, persona_clause=persona_clause,
                        uses_product=uses_product)
                    # The line is not a guard clause — it is the deliverable.
                    # If it did not survive, the whole rewrite is discarded for
                    # this beat: a clip that says something we never wrote is
                    # dead paid work, not a degraded one.
                    if not _omni_line_survived(prompt, beat, pronunciations):
                        on_progress(
                            f"  beat {i} omni prompt lost its spoken line — "
                            f"using the deterministic brief"
                        )
                        prompt, restaged, source, model = (
                            fallback, False, "fallback", None)
            except Exception as e:  # noqa: BLE001 — one beat never aborts the run
                on_progress(
                    f"  beat {i} motion prompt fell back to f-string: {e!r}")
                source = "fallback"
        # Product-lock vocabulary is safety-critical for product beats — same
        # rule as the anatomy guard: never leave it to Opus to paraphrase. If
        # the size/label-adhesion core is missing on a beat that shows the
        # product, append the stability clause verbatim. This is what stops the
        # product rescaling ("size varies") and a transparent-box label
        # detaching/hovering under i2v motion. Kling exempt for the same reason
        # as the anatomy guard — the product bans are in _KLING_NEG_PRODUCT.
        if (mode != "reaction" and not use_kling and uses_product
                and "printed flat" not in prompt.lower()):
            prompt = f"{prompt.rstrip()} {_PRODUCT_STABILITY_CLAUSE}"
        # Faithfulness + skin texture are the other two safety-critical clauses
        # Opus routinely strips when it rewrites a prompt to "motion-only"
        # (verified missing on live paths 2026-07-31): without faithfulness the
        # clip reframes/relights away from its anchor frame; without named fine
        # texture the re-synthesized frames regress to the waxy CGI prior.
        # Kling exempt (its repair appends the faithfulness tail exactly once,
        # and the skin bans ride _KLING_NEG_PERSON); Omni exempt
        # (_omni_safety_tail + _OMNI_REALISM_CLAUSE own that surface).
        if (mode != "reaction" and not use_kling and not omni_mode
                and "composition and colors" not in prompt.lower()):
            prompt = f"{prompt.rstrip()} {_FAITHFULNESS_CLAUSE}"
        if (mode != "reaction" and not use_kling and not omni_mode
                and getattr(beat, "shows_creator", True)
                and "fine natural texture" not in prompt.lower()):
            prompt = f"{prompt.rstrip()} {_SKIN_TEXTURE_CLAUSE}"
        # Fine-motor beat (tying/knotting/threading...): constrain the
        # animation to a cut-on-action insert — the moment is kept, the
        # warp-prone multi-step finger work is not animated. Kling exempt
        # (its positives stay motion-only; warped-hand bans ride
        # _KLING_NEG_PRODUCT/_KLING_NEG_PERSON and its hands micro-motion is
        # already "one quick, precise movement").
        if (mode != "reaction" and not use_kling
                and _DEXTERITY_RE.search(
                    f"{getattr(beat, 'broll_shot', None) or ''} "
                    f"{beat.visual_note or ''}")
                and "multi-step finger" not in prompt.lower()):
            prompt = f"{prompt.rstrip()} {_FINE_MOTOR_CLAUSE}"
        negative_prompt = None
        if use_kling:
            # Re-shape for Kling: Opus's motion text (grounded in the anchor) is
            # the source, the deterministic repair enforces the contract — ≤400
            # chars, no camera language (there is no camera_fixed to lock with),
            # bans moved out to negative_prompt, one faithfulness tail.
            prompt, negative_prompt = _kling_broll_prompt(
                beat, mode=mode,
                motion_source=prompt if source == "opus" else None,
            )
        records[i] = {
            "index": i, "mode": mode, "source": source, "prompt": prompt,
            "anchor_url": anchor.url, "planner_visual_note": beat.visual_note,
            "restaged": restaged, "model": model,
            # The clip stage (and resume) read the provider back off this record
            # instead of re-deriving it — the prompt SHAPE is provider-specific,
            # so the body builder must follow the prompt actually written.
            "provider": ("omni" if omni_mode
                         else "kling" if use_kling else "seedance"),
            "negative_prompt": negative_prompt,
        }
        # asyncio is single-threaded — safe to mutate + persist here.
        manifest.update(
            motion_prompts=[records[k] for k in sorted(records)])

    await asyncio.gather(
        *[_one(i, b, a, w)
          for i, (b, a, w) in enumerate(zip(beats, anchors, windows))]
    )
    if on_asset:
        on_asset("motion_prompts", [records[k] for k in sorted(records)])
    return records


def _clip_request(beat: Any, *, anchor_url: str, duration: int, seed: int,
                  native: bool, prompt: str,
                  audio_url: str | None,
                  resolution: str = "720p",
                  use_kling: bool = False,
                  negative_prompt: str | None = None,
                  omni: bool = False) -> VideoRequest:
    """Video request for one beat.

    Native talking heads route to the reference-to-video model — the only
    EvoLink model that accepts audio_urls (i2v hard-rejects them with a 400)
    — and arrive with the reference voice baked in (generate_audio=True;
    assemble keeps that track). They NEVER send camera_fixed: r2v's
    acceptance of the key is unverified, and a 400 there would kill a
    fully-billed clip stage. Every other beat stays on the default i2v model,
    silent, with the tripod-only camera lock.

    ``use_kling`` (b-roll only, decided by _use_kling in stage 5b) swaps in the
    Kling i2v model + its negative_prompt. duration stays as computed: Kling's
    range is 3-15s, so the 4s Seedance floor is valid there too.

    ``omni`` routes to Gemini Omni, which is not on the EvoLink surface at all:
    it generates the speech itself from the prompt, rejects reference audio, and
    has neither a negative_prompt nor a camera_fixed field. generate_audio is
    unconditionally True there — the voice IS the deliverable, not an option.
    """
    if omni:
        return VideoRequest(
            prompt=prompt, first_frame_url=anchor_url, last_frame_url=None,
            duration=duration, aspect_ratio="9:16", resolution=resolution,
            generate_audio=True, seed=seed, model=OMNI_VIDEO_MODEL,
            camera_fixed=False, audio_url=None, negative_prompt=None,
        )
    if use_kling:
        # Kling's body builder emits only model/image_start/prompt/duration/
        # quality/sound (+ negative_prompt), so seed / camera_fixed /
        # aspect_ratio / generate_audio are dropped on the way out — Kling i2v
        # has none of them (no seed at all: generations are non-reproducible,
        # and the aspect ratio is derived from image_start). audio_url is always
        # None here by construction: _use_kling excludes every audio-driven
        # beat, and _kling_body raises on one rather than silently dropping it.
        return VideoRequest(
            prompt=prompt, first_frame_url=anchor_url, last_frame_url=None,
            duration=duration, aspect_ratio="9:16", resolution=resolution,
            generate_audio=False, seed=None, model=KLING_BROLL_MODEL,
            camera_fixed=False, audio_url=None,
            negative_prompt=negative_prompt,
        )
    return VideoRequest(
        prompt=prompt, first_frame_url=anchor_url, last_frame_url=None,
        duration=duration, aspect_ratio="9:16", resolution=resolution,
        generate_audio=native, seed=seed,
        model=NATIVE_LIPSYNC_MODEL if native else DEFAULT_MODEL_ID,
        camera_fixed=False if native else _camera_fixed(beat),
        audio_url=audio_url,
    )


_OMNI_EVOLINK = "omni_evolink"


@dataclass(frozen=True)
class _ClipFallback:
    """A SECOND route to the same model, used only after the primary fails.

    Today's only instance is Omni: Google's Interactions API is primary and
    EvoLink's resale of gemini-omni-flash is the fallback (see
    config.OMNI_EVOLINK_MODEL). It carries its own ``request`` because the two
    routes do not take the same body — EvoLink wants an image URL and an integer
    duration where Google wants base64 and a "10s" string — and its own
    ``provider`` name so the ledger meters the clip against the surface that
    actually billed it.
    """
    client: Any
    request: VideoRequest
    provider: str


def _tag_route(provider: str, task_id: str) -> str:
    """Stamp a fallback-route task id with the route that issued it.

    One manifest slot (``clip_task_ids[i]``) now has to hold an id from either
    of two APIs, and handing a Google interaction id to EvoLink's /tasks (or the
    reverse) is a guaranteed 400 on the retry path. The id stays a single opaque
    string everywhere downstream — only _order_clip decodes it.
    """
    return f"{provider}:{task_id}"


def _split_route(task_id: str | None) -> tuple[str | None, str | None]:
    """(route, raw_id) for a persisted clip task id.

    An untagged id means the run's PRIMARY route — that is what every manifest
    written before the Omni fallback landed contains, so the absence of a tag
    must keep meaning "primary" forever.
    """
    if task_id and task_id.startswith(f"{_OMNI_EVOLINK}:"):
        return _OMNI_EVOLINK, task_id.split(":", 1)[1]
    return None, task_id


def _make_omni_fallback_client(settings: Settings, omni_mode: bool, *,
                               on_progress: Callable[[str], None]) -> Any:
    """The EvoLink client that backs up Google's Omni route, or None.

    Returns None (and says why) rather than raising when the fallback cannot be
    built: it is a resilience layer, so a missing EVOLINK_API_KEY must degrade
    an Omni run back to "Google or nothing", never block one that would
    otherwise have worked.
    """
    if not omni_mode or not OMNI_EVOLINK_FALLBACK:
        return None
    if not settings.evolink_api_key:
        on_progress("[omni] EVOLINK_API_KEY not set — no fallback route for "
                    "clips Google rejects (a 4xx will kill the run)")
        return None
    from app.video_gen import EvoLinkVideoClient
    on_progress(f"[omni] fallback route armed: {OMNI_EVOLINK_MODEL} on EvoLink "
                f"(~${OMNI_EVOLINK_USD_PER_SECOND:.3f}/s) if Google refuses a clip")
    return EvoLinkVideoClient(settings)


def _omni_fallback(req: VideoRequest, client: Any) -> "_ClipFallback | None":
    """Pair an Omni clip request with its EvoLink twin.

    Only the model slug changes: EvoLink splits Omni's task modes into separate
    models where Google switches them with video_config.task, and _omni_evolink_body
    reads nothing else that differs between the routes (prompt, anchor URL,
    aspect ratio and integer duration are shared). Everything the EvoLink route
    cannot honour — seed, resolution — it drops there rather than here, so the
    two requests stay visibly the same clip.
    """
    if client is None:
        return None
    return _ClipFallback(
        client=client,
        request=replace(req, model=OMNI_EVOLINK_MODEL),
        provider=_OMNI_EVOLINK,
    )


async def _order_clip(video_client: Any, req: VideoRequest, *, index: int,
                      dest: Path,
                      on_task_id: Callable[[str], None],
                      existing_task_id: str | None = None,
                      provider: str = "seedance",
                      used_seconds: float = 0.0,
                      fallback: "_ClipFallback | None" = None,
                      on_progress: Callable[[str], None] | None = None,
                      ) -> tuple[str, str, str]:
    """Order ONE clip: create the task, poll it, download the artifact.

    Shared by the clip stage and by resume's tier 3 so a re-ordered clip is
    billed and fetched by exactly the same code as a first-run clip.
    ``on_task_id`` fires the moment the provider ACCEPTS the job — before the
    poll — because task ids written only on completion made paid work
    unrecoverable after a kill (incident 2026-07-09). ``existing_task_id`` skips
    the (paid) create entirely and polls the already-accepted task — the
    clip-resume path (af30e37). Returns (task_id, video_url, provider_used).

    With a ``fallback``, a clip the primary route refuses is RE-ORDERED on the
    second route instead of killing the run (2026-08-05: Google rejected 3 of a
    10-clip Omni run with a bare 400 `invalid_request`, and _gather_with_retry's
    re-poll of the same interaction could only reproduce it). The fallback is
    tried exactly once, only when the primary raised, and it always creates
    fresh: the failed route's id is worthless to it.

    This is also where the run's clip spend is metered, for the same reason it
    is the shared seam: a clip that _gather_with_retry re-orders after a
    failure was really paid for twice, and billing here counts both. The
    ledger dedupes on task_id, so re-polling an already-completed task (the
    ``existing_task_id`` path) does not invent a second charge. Note what is
    NOT metered: a primary clip that died mid-poll. Whether Google billed the
    rejected interaction is unknown, and this ledger only ever records what a
    provider reported.
    """
    async def _attempt(client: Any, request: VideoRequest, prov: str,
                       existing: str | None) -> tuple[str, str, str]:
        if existing:
            task_id = existing
        else:
            task_id, _ = await client.create_task_async(request)
            on_task_id(task_id if prov == provider
                       else _tag_route(prov, task_id))
        final = None
        async for _status, resp in client.poll_task_stream_async(task_id):
            if resp is not None:
                final = resp
        if final is None:
            raise RuntimeError(f"clip {index} produced no result")
        # Same billed-duration rule the clip records use, evaluated here because
        # `duration_supported` is only known once the client has made the call:
        # an Omni retry that dropped the duration field billed at the 10s
        # default. Only Google's route has that quirk — EvoLink takes an integer.
        ordered = float(request.duration or 0)
        if prov == "omni" and not getattr(client, "duration_supported", True):
            ordered = float(OMNI_DUR_MAX)
        # A completed EvoLink task carries usage.cost.usd — the amount EvoLink
        # actually billed. Metered before the download so a CDN failure cannot
        # lose a charge that was already incurred.
        costs.record_clip(
            task_id=task_id, provider=prov, model=request.model,
            ordered_seconds=ordered, used_seconds=used_seconds,
            raw=getattr(final, "raw", None),
        )
        # Off the event loop: a synchronous CDN download here blocks EVERY
        # concurrent poller for its full duration — with a degraded CDN this
        # wedged the whole gather (nobody polls, no deadline fires, silence).
        await asyncio.to_thread(download, final.video_url, dest)
        return task_id, final.video_url, prov

    route, existing = _split_route(existing_task_id)
    if route is not None:
        # This clip was already re-ordered on the fallback route once (its
        # create was accepted and persisted, then something downstream failed).
        # Polling the primary with an id it never issued is a certain 400.
        if fallback is None or fallback.provider != route:
            raise RuntimeError(
                f"clip {index} has a {route} task id ({existing}) but that "
                f"route is not available on this run — re-run with the "
                f"fallback enabled, or clear the id to re-order the clip"
            )
        return await _attempt(fallback.client, fallback.request, route, existing)
    try:
        return await _attempt(video_client, req, provider, existing)
    except Exception as e:  # noqa: BLE001 — any primary failure is the trigger
        if fallback is None:
            raise
        msg = (f"  clip {index + 1}: {provider} route failed "
               f"({type(e).__name__}: {str(e)[:200]}) — re-ordering on "
               f"{fallback.provider} ({fallback.request.model})")
        (on_progress or print)(msg)
        return await _attempt(fallback.client, fallback.request,
                              fallback.provider, None)


async def _finish_run(
    *, beats: list, windows: list, clip_paths: list[str],
    vo_audio_path: str | None, alignment: Alignment | None,
    lipsync_client: Any, uploader: Any, voice: Any, settings: Settings,
    work: Path, out_path: str | Path, manifest: _Manifest,
    lipsync_engine: str, lipsync_granularity: str, isolated_audio: bool,
    omni_mode: bool = False,
    beat_audio_paths: list[str | None] | None,
    beat_audio_durs: list[float | None] | None,
    beat_alignments: list[Any] | None,
    product_ref_url: str | None, product_name: str | None,
    clip_resolution: str, burn_captions: bool, finishing_grade: bool,
    finishing_look: str, color_match: bool, visual_qc: bool,
    room_tone: bool, transition_style: str, punch_in_accent: bool,
    on_progress: Callable[[str], None],
    on_asset: Callable[[str, Any], None] | None,
) -> Any:
    """Stage 7 + finishing: lipsync → concat → master → QC → persist.

    Extracted so resume_ugc_video finishes a run through the SAME tail as a
    fresh run — a forked copy would drift (the assemble() call alone carries 25
    behavioural flags, and any one of them silently differing between the two
    paths is a bug you only see in the rendered master). Everything before this
    point differs between the two entry points (generate vs. rehydrate); from
    here they are identical. Returns the AssembledVideo.
    """
    whole_video_lipsync = (
        lipsync_engine == "syncso" and lipsync_granularity == "whole"
    )
    # Omni clips arrive with their speech already baked in and no lipsync pass
    # ever runs — structurally the same contract as seedance_native (speech-end
    # trim, per-clip audio, master the concat's own mix, captions recovered by
    # force-aligning the known script against the finished video), so it rides
    # the same assemble() mode rather than a new one.
    native_lipsync = lipsync_engine == "seedance_native" or omni_mode
    if omni_mode:
        _assemble_desc = (
            "assembling (omni: speech-end trim → per-clip audio → concat)...")
    elif lipsync_engine == "seedance_native":
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
    on_progress(_assemble_desc)
    # The product-reveal punch-in lands on the FIRST product-bearing b-roll
    # beat (product appears in solution/proof/cta by UGC anatomy, so this is
    # the reveal). _beat_uses_product() is the single source of truth.
    punch_in_beat = None
    if punch_in_accent:
        punch_in_beat = next(
            (i for i, b in enumerate(beats)
             if b.shot_type == "broll"
             and _beat_uses_product(b, product_ref_url, product_name)),
            None,
        )
    if alignment is None:
        # Omni mode has no VO clock at all — the model authors each clip's
        # speech, so nothing was force-aligned before the render. assemble()
        # dereferences the alignment only for its drift LOG and as the
        # final-duration fallback when the concat probe fails, so a
        # one-character alignment spanning the planned timeline covers every
        # use. It is also EXACTLY what resume rebuilds from vo_duration_s, so
        # both entry points hand assemble() the same shape of object.
        alignment = Alignment(characters=["x"], starts=[0.0],
                              ends=[float(windows[-1][1]) if windows else 0.0])
    assembled = await assemble(
        beats=beats, windows=windows, clip_paths=clip_paths,
        vo_audio_path=vo_audio_path, alignment=alignment,
        lipsync_client=lipsync_client, uploader=uploader,
        work_dir=work / "assembly", out_path=out_path,
        on_asset=on_asset, manifest=manifest,
        native_lipsync=native_lipsync,
        # Omni authors audio for EVERY clip (its silent-beat briefs order the
        # scene's room tone by name), so non-speaking beats keep their own
        # ambience. Seedance i2v clips are audio-less — nothing to keep.
        native_ambient_audio=omni_mode,
        isolated_audio=isolated_audio,
        beat_audio_paths=(beat_audio_paths if isolated_audio else None),
        beat_audio_durs=(beat_audio_durs if isolated_audio else None),
        whole_video_lipsync=whole_video_lipsync,
        # Concurrency is per PROVIDER (rate limits), not per topology.
        lipsync_concurrency=(REPLICATE_MAX_CONCURRENCY
                             if lipsync_engine == "heygen_replicate"
                             else SYNC_MAX_CONCURRENCY),
        # Reuse of already-PAID lipsync results. The `hasattr(manifest, "get")`
        # guard that used to sit here was dead code — _Manifest had no `get`, so
        # this argument was always None and a resumed/retried run re-paid for
        # every talking head. _Manifest.get exists now; call it directly.
        prior_lipsync_records=manifest.get("lipsync"),
        clip_resolution=clip_resolution,
        burn_captions=burn_captions,
        finishing_grade=finishing_grade,
        room_tone=room_tone,
        transition_style=transition_style,
        punch_in_beat=punch_in_beat,
        beat_alignments=(beat_alignments if isolated_audio else None),
        caption_words_per_line=CAPTION_WORDS_PER_LINE,
        # Caption keyword emphasis rides the planner's product_name; older
        # timelines without the field degrade to no emphasis.
        emphasis_terms=tuple(product_name.split()) if product_name else (),
        # Native mode has no trustworthy TTS timing (r2v re-renders speech) —
        # captions recover the true clock by force-aligning the known script
        # against the assembled audio. getattr: injected fakes/older clients
        # without the capability degrade to captions-skipped, never a crash.
        force_align=getattr(voice, "force_align", None),
        loudnorm_i=LOUDNORM_I,
        finishing_look=finishing_look,
        color_match=color_match,
    )
    # Report-only AI review (defect audit) of the finished master. Best
    # effort by design: a review failure must never fail a paid render.
    if visual_qc:
        on_progress("AI review (Gemini defect audit) of the finished ad...")
        try:
            from app.qc import review_video, summarize  # noqa: PLC0415
            qc_report = await review_video(
                assembled.path, settings, duration=assembled.duration_s)
            manifest.update(qc_report=qc_report)
            on_progress("  " + summarize(qc_report))
            for issue in (qc_report.get("issues") or []):
                if issue.get("severity", 0) >= 3:
                    on_progress(
                        f"  [sev{issue['severity']} {issue['category']} "
                        f"{issue['timestamp']}] {issue['issue']} → "
                        f"{issue['suggested_fix']}"
                    )
            if on_asset is not None:
                on_asset("qc_report", qc_report)
        except Exception as exc:  # noqa: BLE001 — QC is best-effort
            on_progress(f"  AI review skipped: {type(exc).__name__}: {exc}")
    on_progress(
        f"DONE → {assembled.path} ({assembled.duration_s:.1f}s, "
        f"{assembled.n_lipsynced} lipsynced beats)"
    )
    final_s3_url = await _try_upload_deliverable(
        uploader, assembled.path, on_progress=on_progress)
    manifest.update(
        final_video_path=assembled.path,
        final_video_s3_url=final_s3_url,
        duration_s=assembled.duration_s,
        n_lipsynced=assembled.n_lipsynced,
    )
    manifest.add_step("final", produced={
        "local_path": str(assembled.path),
        "s3_url": final_s3_url,
        "duration_s": round(assembled.duration_s, 3),
        "n_lipsynced": assembled.n_lipsynced,
    })
    # What the run actually cost, from what each provider reported. Written
    # here because _finish_run is the one exit both a fresh run and a resume
    # pass through, so neither can produce a video with no cost record.
    #
    # A resume is a SECOND pass over the same run dir, and its ledger holds
    # only what that pass spent. Appending to `cost_passes` rather than
    # replacing `cost` keeps the original run's spend — the manifest is the
    # record of everything this directory cost, and a resume must not erase
    # the money that produced the clips it just reused.
    ledger = costs.active()
    if ledger is not None:
        snapshot = ledger.snapshot()
        # getattr guard: test doubles inject manifests without a `get`.
        prior = (manifest.get("cost_passes")
                 if hasattr(manifest, "get") else None)
        passes = list(prior or [])
        passes.append(snapshot)
        manifest.update(
            cost=snapshot,
            cost_passes=passes,
            cost_total_usd=round(sum(p["total_usd"] for p in passes), 4),
        )
        on_progress(costs.format_line(snapshot))
        if on_asset is not None:
            on_asset("cost", snapshot)
    manifest.mark("complete")
    return assembled


@dataclass
class PipelineResult:
    final_video_path: str
    timeline: Any
    # None in omni mode: there is no separate voiceover file — the speech only
    # ever exists inside the clips (and then inside the master).
    vo_audio_path: str | None
    character_ref_url: str
    duration_s: float
    n_lipsynced: int


async def generate_ugc_video(
    *, brief: str, target_duration_s: int | None = None,
    voice_id: str | None = None,
    settings: Settings, work_dir: str | Path, out_path: str | Path,
    product_ref_url: str | None = None,
    hook_type: str | None = None,
    narrative_structure: str | None = None,
    lipsync_engine: str = DEFAULT_LIPSYNC_ENGINE,
    lipsync_granularity: str = SYNC_LIPSYNC_GRANULARITY,
    llm_provider: str | None = None,
    trim_silence: bool = True,
    clip_resolution: str = DEFAULT_CLIP_RESOLUTION,
    broll_video_provider: str = BROLL_VIDEO_PROVIDER,
    burn_captions: bool = BURN_CAPTIONS,
    finishing_grade: bool = FINISHING_GRADE,
    finishing_look: str = FINISHING_LOOK,
    color_match: bool = COLOR_MATCH_CLIPS,
    visual_qc: bool = VISUAL_QC,
    room_tone: bool = ROOM_TONE,
    transition_style: str = TRANSITION_STYLE,
    punch_in_accent: bool = PUNCH_IN_ACCENT,
    use_voice_library: bool | None = None,
    voice_provider: str = VOICE_PROVIDER,
    video_generator: str = VIDEO_GENERATOR,
    on_progress: Callable[[str], None] = print,
    on_asset: Callable[[str, Any], None] | None = None,
) -> PipelineResult:
    # Before the argument checks: nothing below this line is free, and a run
    # that dies mid-flight should still have metered whatever it already paid.
    costs.start_run()
    if video_generator not in VIDEO_GENERATORS:
        raise ValueError(
            f"unknown video_generator {video_generator!r} — "
            f"expected one of {VIDEO_GENERATORS}"
        )
    omni_mode = video_generator == "omni"
    if omni_mode:
        # Omni authors video AND voice in one pass: the TTS/lipsync/b-roll
        # knobs have no effect, and the API is 720p-fixed. Normalize loudly
        # rather than silently — the operator should see what their toggles do.
        if clip_resolution != "720p":
            on_progress(
                f"[omni] clip_resolution {clip_resolution!r} clamped to 720p "
                "(Omni's API is 720p-only)"
            )
            clip_resolution = "720p"
        on_progress(
            "[omni] end-to-end mode: voice is PROMPTED (no TTS provider), "
            "no lipsync pass, b-roll provider ignored"
        )
    if lipsync_engine not in _LIPSYNC_ENGINES:
        raise ValueError(
            f"unknown lipsync_engine {lipsync_engine!r} — "
            f"expected one of {sorted(_LIPSYNC_ENGINES)}"
        )
    if transition_style not in TRANSITION_STYLES:
        raise ValueError(
            f"unknown transition_style {transition_style!r} — "
            f"expected one of {TRANSITION_STYLES}"
        )
    if broll_video_provider not in BROLL_VIDEO_PROVIDERS:
        raise ValueError(
            f"unknown broll_video_provider {broll_video_provider!r} — "
            f"expected one of {BROLL_VIDEO_PROVIDERS}"
        )
    if finishing_look not in FINISHING_LOOKS:
        raise ValueError(
            f"unknown finishing_look {finishing_look!r} — "
            f"expected one of {FINISHING_LOOKS}"
        )
    if lipsync_granularity not in SYNC_GRANULARITIES:
        raise ValueError(
            f"unknown lipsync_granularity {lipsync_granularity!r} — "
            f"expected one of {sorted(SYNC_GRANULARITIES)}"
        )
    if voice_provider not in VOICE_PROVIDERS:
        raise ValueError(
            f"unknown voice_provider {voice_provider!r} — "
            f"expected one of {sorted(VOICE_PROVIDERS)}"
        )
    # "whole" sends ONE Sync.so pass over the stitched cut — only the syncso engine
    # supports it. HeyGen needs per-clip audio muxing; seedance_native is
    # audio-driven (no post-hoc pass), so there is no whole-video step to run.
    if lipsync_granularity == "whole" and lipsync_engine != "syncso":
        raise ValueError(
            f"lipsync_granularity='whole' is only supported with "
            f"lipsync_engine='syncso' (got {lipsync_engine!r})."
        )
    llm = llm_provider or settings.llm_provider
    if llm not in _LLM_PROVIDERS:
        raise ValueError(
            f"unknown llm_provider {llm!r} — "
            f"expected one of {sorted(_LLM_PROVIDERS)}"
        )
    if llm == "claude" and not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example)."
        )
    # Non-ElevenLabs providers generate audio only; the char-level master clock
    # is recovered with ElevenLabs Forced Alignment (and the QC gate uses Scribe),
    # so the key is required on EVERY provider. Fail here rather than after the
    # first paid TTS call. Omni mode is the exception: there is no TTS clock at
    # all — ElevenLabs is used only to force-align captions against the finished
    # video, and captions already degrade gracefully when that fails.
    if not settings.elevenlabs_api_key:
        if omni_mode:
            on_progress(
                "[omni] ELEVENLABS_API_KEY is not set — captions will be "
                "skipped (force-alignment against the finished video needs it)"
            )
        else:
            raise RuntimeError(
                "ELEVENLABS_API_KEY is not set. Add it to .env (see .env.example). "
                "It is required on every voice provider — the pipeline's timing "
                "clock comes from ElevenLabs Forced Alignment."
            )
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    # Manifest is the resumable record of everything generated. Atomic-written
    # at every milestone so a crashed/slept run still preserves all URLs +
    # paths to the work that was already paid for.
    manifest = _Manifest(work / "manifest.json")
    manifest.update(
        brief=brief,
        params={
            "target_duration_s": target_duration_s,
            "voice_id_override": voice_id,
            "voice_provider": voice_provider,
            "product_ref_url": product_ref_url,
            "hook_type": hook_type,
            "narrative_structure": narrative_structure,
            "lipsync_engine": lipsync_engine,
            "lipsync_granularity": lipsync_granularity,
            "llm_provider": llm,
            "trim_silence": trim_silence,
            "clip_resolution": clip_resolution,
            "broll_video_provider": broll_video_provider,
            "video_generator": video_generator,
        },
        out_path=str(out_path),
    )

    gem = GeminiClient(
        settings,
        image_skill_md_path=ROOT / "docs/character-image-skill.md",
        skill_md_path=ROOT / "video-seedance-skill.md",
        omni_skill_md_path=ROOT / "video-omni-skill.md",
        llm_provider=llm,
    )
    # Omni mode has no TTS; the voice client survives ONLY as the captions
    # force-aligner (and only when the key exists — checked above).
    if omni_mode:
        voice = (make_voice_client(settings, "elevenlabs")
                 if settings.elevenlabs_api_key else None)
    else:
        voice = make_voice_client(settings, voice_provider)
    uploader = make_uploader(settings)
    image_client = make_image_client(settings, uploader)
    video_client = make_video_client(settings, video_generator)
    # Omni's second route, built once for the whole clip fan-out. Constructing it
    # up front (not lazily on first failure) means a missing EVOLINK_API_KEY
    # surfaces before a single paid clip, not 8 clips into the stage.
    omni_fallback_client = _make_omni_fallback_client(settings, omni_mode,
                                                     on_progress=on_progress)
    # No lipsync pass exists in omni mode — don't demand its keys either.
    lipsync_client = None if omni_mode else make_lipsync_client(settings, lipsync_engine)

    # Isolated per-beat audio topology: each speaking beat gets its OWN
    # ElevenLabs TTS, that audio drives the clip duration, and the synced
    # audio stays baked in (no continuous-VO overlay → no cross-clip drift).
    # heygen_replicate always; syncso in per_clip granularity. The legacy
    # continuous-VO slicing survives only for the direct heygen engine and
    # syncso "whole".
    isolated_audio = not omni_mode and (
        lipsync_engine == "heygen_replicate"
        or (lipsync_engine == "syncso" and lipsync_granularity == "per_clip")
    )

    # 0. Brief directives — extract the client's EXPLICIT instructions once,
    # up front: a client-written script becomes a code-enforced verbatim
    # contract in plan_timeline, and visual directives survive past the
    # planner to the per-beat motion writers. Fail-open: extraction is an
    # adherence aid, never a reason to kill a run.
    directives = None
    try:
        directives = await gem.extract_brief_directives(brief=brief)
        if directives.explicit_script:
            on_progress(
                f"brief contains an explicit script "
                f"({len(directives.explicit_script.split())} words) — "
                f"verbatim mode FORCED"
            )
        if directives.visual_directives:
            on_progress(
                f"brief has {len(directives.visual_directives)} explicit "
                f"visual directive(s) — threading to planner + prompt writers"
            )
        manifest.update(brief_directives=directives.model_dump())
    except Exception as exc:  # noqa: BLE001
        on_progress(f"brief-directive extraction failed ({exc!r}) — "
                    f"planning from the raw brief")

    # 0b. Product grounding — the planner stages product shots BLIND (it gets
    # only product_present), which is where hallucinated container types came
    # from. One cheap vision call turns the photo into a factual sentence the
    # planner writes against. Fail-open, from the RAW photo (normalization
    # happens later and only cleans the background).
    product_description = ""
    if product_ref_url:
        try:
            product_description = await gem.describe_product(
                product_ref_url=product_ref_url)
            on_progress(f"product identified: {product_description}")
            manifest.update(product_description=product_description)
        except Exception as exc:  # noqa: BLE001
            on_progress(f"product description failed ({exc!r}) — planner "
                        f"stages product shots from the brief alone")

    # 1. Character description — cast from the BRIEF alone, BEFORE the director,
    # so the SCENE-FIRST plan_timeline can stage every visual_note for this
    # actual creator + setting (and it picks the voice_vibe for the VO).
    on_progress("describing character (also picks voice_vibe)...")
    character = await gem.describe_character(brief=brief)
    character_dump = character.model_dump()
    manifest.update(character=character_dump)
    manifest.add_step("character", produced=character_dump)
    if on_asset:
        on_asset("character", character_dump)

    # 2. Timeline ---------------------------------------------------------
    on_progress("planning timeline...")
    timeline = await gem.plan_timeline(
        brief=brief, character=character,
        product_present=product_ref_url is not None,
        target_duration_s=target_duration_s,
        hook_type=hook_type, narrative_structure=narrative_structure,
        directives=directives, product_description=product_description,
    )
    on_progress(
        f"timeline: {len(timeline.beats)} beats "
        f"({timeline.narrative_structure}/{timeline.hook_type}), "
        f"{len(timeline.full_script.split())} words"
    )
    timeline_dump = timeline.model_dump()
    manifest.update(timeline=timeline_dump)
    manifest.add_step("timeline", produced={
        "full_script": timeline.full_script,
        "narrative_structure": timeline.narrative_structure,
        "hook_type": timeline.hook_type,
        "n_beats": len(timeline.beats),
    })
    if on_asset:
        on_asset("timeline", timeline_dump)

    # Resolve voice_id. Precedence:
    #   1. explicit user override (voice_id arg),
    #   2. best-matching CLONED voice from the library (character-first: the
    #      character is already cast from the brief; we pick the real voice that
    #      fits it — fixes the "voice didn't match the character" complaint),
    #   3. curated catalog by (gender, vibe) — the fallback when the library is
    #      off/empty or no cloned voice genuinely fits (e.g. older character vs
    #      an all-early-20s clone set).
    library_on = (USE_CLONED_VOICE_LIBRARY if use_voice_library is None
                  else use_voice_library)
    if omni_mode:
        # No TTS voice exists to resolve: the voice IS the persona clause,
        # rendered verbatim into every clip prompt. Record that so the
        # manifest never carries a misleading catalog/clone id.
        library_on = False
        voice_source = "prompted"
        voice_match_reason = "omni-native voice (described in prompt)"
    else:
        voice_source = "override" if voice_id else "catalog"
        voice_match_reason = ""
    resolved_voice_id = None if omni_mode else voice_id
    if not omni_mode and not resolved_voice_id and library_on:
        from app.voice_library import candidates_for_gender, load_voice_library
        # Provider-scoped: only voices actually cloned on `voice_provider` are
        # offered to the matcher, so it can never pick a voice this engine
        # cannot speak. An empty pool falls through to the catalog below.
        profiles = load_voice_library(provider=voice_provider)
        candidates = candidates_for_gender(profiles, character.gender)
        if candidates:
            match = await gem.match_voice(character=character, profiles=candidates)
            if match.voice_id:
                resolved_voice_id = match.voice_id
                voice_source = "cloned_library"
                voice_match_reason = match.reason
            else:
                voice_match_reason = match.reason  # why we fell back
    if not omni_mode and not resolved_voice_id:
        # The 8-vibe catalog holds ELEVENLABS voice_ids — they are meaningless to
        # Cartesia/Fish (which would 404 voice_not_found). So the catalog is only
        # a legal fallback on ElevenLabs. On another provider, an empty candidate
        # pool means its clones are missing: say so and stop, rather than
        # silently switching engines behind the user's back or spending on a
        # request that cannot succeed.
        if voice_provider != "elevenlabs":
            from app.voice_library import provider_id_field
            raise RuntimeError(
                f"voice_provider={voice_provider!r} has no cloned voice for a "
                f"{character.gender} character (looked for "
                f"{provider_id_field(voice_provider)!r} in the voice library). "
                f"Run: uv run python scripts/clone_providers.py "
                f"--provider {voice_provider}  — or pass an explicit "
                f"{voice_provider} voice id as the override. The 8-vibe catalog "
                "cannot be used here: those ids are ElevenLabs-only."
            )
        resolved_voice_id = voice_for(character.gender, character.voice_vibe)
    on_progress(
        f"voice → {resolved_voice_id or 'omni-native'} "
        f"(provider={'omni-native' if omni_mode else voice_provider}, "
        f"source={voice_source}, "
        f"gender={character.gender}, vibe={character.voice_vibe}"
        f"{'; ' + voice_match_reason if voice_match_reason else ''})"
    )
    voice_info = {
        "voice_id": resolved_voice_id,
        "provider": "omni-native" if omni_mode else voice_provider,
        "gender": character.gender,
        "vibe": character.voice_vibe,
        "source": voice_source,
        "match_reason": voice_match_reason,
        "override": bool(voice_id),
    }
    manifest.update(voice=voice_info)
    if on_asset:
        on_asset("voice", voice_info)

    # Register this run's pronunciation respellings BEFORE any TTS. On
    # ElevenLabs these become an `alias` dictionary (server-side substitution —
    # the alignment keeps the original spelling, so the master clock and captions
    # are untouched); on Cartesia/Fish they are substituted into the sent text.
    pronunciations = list(getattr(timeline, "pronunciations", []) or [])
    if pronunciations:
        setter = getattr(voice, "set_pronunciations", None)
        if setter is not None:
            setter(pronunciations)
        on_progress(
            "pronunciation fixes: "
            + ", ".join(f"{p.word}->{p.respelling}" for p in pronunciations[:6])
            + (f" (+{len(pronunciations) - 6} more)"
               if len(pronunciations) > 6 else "")
        )
    manifest.update(pronunciations=[
        {"word": p.word, "respelling": p.respelling} for p in pronunciations])

    # 3. VO + character-ref prompt in parallel (both depend on character).
    # Omni mode has no VO: the model authors the speech per clip, so only the
    # character-ref prompt is generated here and the clock is synthetic (below).
    if omni_mode:
        on_progress("generating char ref prompt (omni: no TTS voiceover)...")
        char_ref_prompt = await gem.write_character_prompt(
            brief=brief, character=character)
        vo = None
    else:
        on_progress("generating voiceover (ElevenLabs) + char ref prompt...")
        char_ref_prompt, vo = await asyncio.gather(
            gem.write_character_prompt(brief=brief, character=character),
            voice.generate_vo(
                script=timeline.full_script, voice_id=resolved_voice_id,
                out_path=work / "vo.mp3",
                voice_vibe=character.voice_vibe,
            ),
        )
    # Total-duration control: the planner's word budget is a PREDICTION; the
    # VO is ground truth. If the read overshot a FORCED target by >15%, re-plan
    # ONCE with this exact script+voice's MEASURED words/sec and re-voice —
    # one cheap Gemini + ElevenLabs round, before any paid image/video render.
    # (A measured run: 77 words @1.78 w/s → 43.3s for a 30s target.)
    # Skipped when the duration is AI-decided (target None — nothing to hold to)
    # or the script is verbatim (the creative team's length is final).
    if (not omni_mode
            and target_duration_s is not None
            and timeline.script_source != "verbatim"
            and vo.alignment.duration > target_duration_s * 1.15):
        spoken_words = len(_strip_tags(timeline.full_script).split())
        measured_wps = spoken_words / vo.alignment.duration
        on_progress(
            f"VO overshot: {vo.alignment.duration:.1f}s vs {target_duration_s}s "
            f"target ({spoken_words} words @ {measured_wps:.2f} w/s measured) — "
            f"re-planning once with the measured pacing..."
        )
        timeline = await gem.plan_timeline(
            brief=brief, character=character,
            product_present=product_ref_url is not None,
            target_duration_s=target_duration_s,
            hook_type=hook_type, narrative_structure=narrative_structure,
            words_per_second=measured_wps,
        )
        manifest.update(
            timeline=timeline.model_dump(),
            duration_correction={
                "first_vo_s": round(vo.alignment.duration, 3),
                "measured_wps": round(measured_wps, 3),
            },
        )
        if on_asset:
            on_asset("timeline", timeline.model_dump())
        vo = await voice.generate_vo(
            script=timeline.full_script, voice_id=resolved_voice_id,
            voice_vibe=character.voice_vibe,
            out_path=work / "vo.mp3",
        )
        if vo.alignment.duration > target_duration_s * 1.25:
            on_progress(
                f"WARNING: VO still {vo.alignment.duration:.1f}s after the "
                f"correction pass — proceeding (video will run long)"
            )

    if omni_mode:
        # Synthetic clock: no VO exists. Windows are word-budget estimates —
        # the request-sizing input, not ground truth. Reality is recovered per
        # clip at assembly (speech-end trim) and captions via force-align
        # against the finished video, so precision here buys nothing.
        vo_audio_path, alignment = None, None
        beats, windows, n_split = _omni_windows(
            timeline.beats, product_ref_url=product_ref_url,
            product_name=getattr(timeline, "product_name", None),
            on_progress=on_progress)
        if n_split:
            on_progress(
                f"[omni] split {n_split} over-long beat(s) into jump-cut "
                f"sub-beats"
            )
        vo_duration = sum(e - s for s, e in windows)
    else:
        # The VO is final from here on. Dead air must be trimmed NOW — before
        # any window/slice/clip exists — because retiming after lipsync would
        # desync every mouth (see app/pacing.py). The paced path + alignment
        # replace the raw VO for every downstream consumer.
        vo_audio_path, alignment = vo.audio_path, vo.alignment
        if trim_silence:
            pre_trim_s = alignment.duration
            vo_audio_path, alignment = trim_silences(
                vo_audio_path, alignment, work_dir=work,
            )
            manifest.update(pacing={
                "pre_duration_s": round(pre_trim_s, 3),
                "post_duration_s": round(alignment.duration, 3),
            })
            if alignment.duration < pre_trim_s:
                on_progress(
                    f"trimmed VO dead air: {pre_trim_s:.2f}s → "
                    f"{alignment.duration:.2f}s"
                )

        windows = beat_windows(timeline.beats, alignment, timeline.full_script)
        # Deterministic fast-cut enforcement: any window over ~5s (TTS pacing
        # variance, tag pauses, the one held beat overrunning) is split at word
        # boundaries into equal ~3s jump-cut sub-beats — each gets its own
        # anchor + clip downstream. From here on, `beats` (not timeline.beats)
        # is the source of truth.
        beats, windows = split_long_windows(timeline.beats, windows, alignment)
        n_split = len(beats) - len(timeline.beats)
        if n_split:
            on_progress(
                f"split {n_split} over-long beat window(s) into jump-cut "
                f"sub-beats"
            )
        if lipsync_engine == "seedance_native":
            # Seedance reference audio needs ≥~2s of speech to land a readable
            # mouth — a shorter talking-head window folds into its neighbour.
            n_before = len(beats)
            beats, windows = merge_short_windows(beats, windows)
            n_merged = n_before - len(beats)
            if n_merged:
                on_progress(
                    f"merged {n_merged} short talking-head window(s) for "
                    f"native lipsync"
                )

        # Reaction holds: splice silent screen time into the VO timeline AFTER
        # the speech windows are final. apply_holds returns the silence-padded
        # VO, the added-cumulative-hold alignment, and ALL beats' windows
        # (reaction beats get their real silent span); it's a clean no-op when
        # no reaction beat exists. Everything downstream rides these.
        n_reactions = sum(
            1 for b in beats
            if getattr(b, "beat_kind", "speech") == "reaction"
            and (getattr(b, "hold_s", None) or 0.0) > 0.0
        )
        if n_reactions:
            vo_audio_path, alignment, windows = apply_holds(
                beats, windows, alignment, vo_audio_path, work_dir=work,
            )
            on_progress(
                f"inserted {n_reactions} reaction hold(s); "
                f"VO now {alignment.duration:.1f}s"
            )
        vo_duration = alignment.duration
    on_progress(
        f"{'planned' if omni_mode else 'VO'} {vo_duration:.1f}s; "
        f"{len(windows)} beat windows"
    )
    clock_state = dict(
        vo_audio_path=vo_audio_path,
        vo_duration_s=vo_duration,
        beat_windows=[{"start": s, "end": e} for s, e in windows],
        n_split_beats=n_split,
        # Resume state. `timeline.beats` is the PLANNER's list — after
        # split_long_windows / merge_short_windows / apply_holds the effective
        # beats (and the alignment they were timed against) can differ, so both
        # are persisted here, once they are final. sub_index is carried
        # explicitly: split_long_windows sets it with object.__setattr__, which
        # model_dump() does not see.
        beats=[_beat_state(b) for b in beats],
    )
    if alignment is not None:
        # Omni mode deliberately writes NO vo_alignment — resume's synthetic-
        # alignment path handles its absence, and a fake clock persisted as a
        # real one would silently re-enable alignment-timed captions.
        clock_state["vo_alignment"] = alignment.to_dict()
    manifest.update(**clock_state)
    if omni_mode:
        manifest.add_step("voiceover", produced={
            "mode": "omni-native",
            "planned_duration_s": round(vo_duration, 3),
        })
    else:
        vo_s3_url = await _try_upload_deliverable(
            uploader, vo_audio_path, on_progress=on_progress)
        manifest.update(vo_s3_url=vo_s3_url)
        manifest.add_step("voiceover", produced={
            "local_path": str(vo_audio_path),
            "s3_url": vo_s3_url,
            "duration_s": round(vo_duration, 3),
            "voice_id": resolved_voice_id,
        })
        if on_asset:
            on_asset("vo_audio", vo_audio_path)

    # Build the single timbre anchor (seedance_native only). The VO + windows
    # are final here, so we slice the head once and upload it once. The same
    # URL is captured by the _clip closure below and passed as audio_url to
    # every speaking clip — that is what keeps voice consistent across cuts
    # without per-beat slicing + upload cost.
    timbre_anchor_url: str | None = None
    # `and not omni_mode` like every other seedance_native site: the lipsync
    # radio's value still arrives on an omni run (the UI only greys it out), and
    # there is no VO to slice an anchor from — ffmpeg would be handed "None"
    # after the run's LLM work was already paid for.
    if lipsync_engine == "seedance_native" and not omni_mode:
        timbre_anchor_url = await asyncio.to_thread(
            build_timbre_anchor, vo_audio_path, work, uploader)
        manifest.update(timbre_anchor_url=timbre_anchor_url)
        on_progress("built single timbre anchor (native voice consistency)")

    # Per-beat isolated TTS (isolated-audio engines: heygen_replicate, syncso
    # per_clip). Each speaking beat gets its OWN ElevenLabs TTS of its FULL
    # dialogue_text — same voice_id for consistency, but independently
    # generated (no shared VO slicing, so no cross-clip A/V drift). The
    # isolated audio duration IS the timing source for these engines:
    # clip_dur = max(4, ceil(isolated_audio_duration)).
    # Silent beats (resolve_voice_mode == "silent") get no audio.
    beat_audio_paths: list[str | None] = [None] * len(beats)
    beat_audio_durs: list[float | None] = [None] * len(beats)
    # Per-beat alignments feed caption timing: isolated mode's final timeline
    # is these TTS clips back-to-back, so the continuous-VO alignment can NOT
    # time captions — each beat's own alignment (shifted by its concat
    # offset) can, exactly.
    beat_alignments: list[Any] = [None] * len(beats)
    if isolated_audio:
        on_progress(
            f"generating {len(beats)} per-beat isolated audio clips "
            f"({voice_provider})..."
        )

        # Every provider enforces a per-account CONCURRENCY cap and returns 429
        # when exceeded (ElevenLabs "concurrent_limit_exceeded"; Cartesia
        # error_code="concurrency_limited", plan limits as low as 2; Fish 5 on
        # the starter tier) — bound the fan-out to the selected provider's limit.
        tts_sem = asyncio.Semaphore(max(1, _TTS_CONCURRENCY[voice_provider]))

        async def _beat_audio(i: int, beat: Any) -> None:
            mode = resolve_voice_mode(beat)
            if mode == "silent":
                return
            out = work / f"beat-{i}.mp3"
            async with tts_sem:
                result = await voice.generate_vo(
                    script=_strip_tags(beat.dialogue_text or "").strip(),
                    voice_id=resolved_voice_id, out_path=out,
                    voice_vibe=character.voice_vibe,
                )
            beat_audio_paths[i] = str(out)
            beat_audio_durs[i] = result.alignment.duration
            beat_alignments[i] = result.alignment

        await asyncio.gather(*[_beat_audio(i, b) for i, b in enumerate(beats)])
        n_speaking = sum(1 for d in beat_audio_durs if d is not None)
        on_progress(
            f"per-beat audio done: {n_speaking} speaking beats "
            f"(min {min((d for d in beat_audio_durs if d is not None), default=0):.1f}s, "
            f"max {max((d for d in beat_audio_durs if d is not None), default=0):.1f}s)"
        )
        manifest.update(beat_audio_paths=beat_audio_paths,
                        beat_audio_durs=beat_audio_durs,
                        beat_alignments=[a.to_dict() if a else None
                                         for a in beat_alignments])

    # 4. Render character reference (Nano Banana Pro).
    on_progress("rendering character reference (Nano Banana Pro)...")
    char_ref = (await image_client.generate_async(
        prompt=char_ref_prompt, tier="hero", source="workflow",
    ))[0]
    manifest.update(character_ref_url=char_ref.url)
    manifest.add_step("char_ref_image", prompt=char_ref_prompt,
                      produced={"url": char_ref.url})
    if on_asset:
        on_asset("char_ref", char_ref.url)

    # Product normalization: branded products hallucinate under text-to-image,
    # so we run a Gemini EDIT of the user's photo first — pixel-accurate
    # label/logo/text preserved, packaging removed — and reassign
    # product_ref_url to that clean bottle so every downstream anchor cites a
    # canonical reference. On failure we fall back to the original photo rather
    # than kill the (paid) run. Skipped entirely when no product is supplied.
    if product_ref_url:
        on_progress("normalizing product reference (Gemini edit → clean bottle)...")
        normalized_url = await _normalize_product_ref(
            image_client, product_ref_url, on_progress=on_progress,
        )
        if normalized_url:
            manifest.update(
                product_ref_normalized=normalized_url,
                product_ref_original=product_ref_url,
            )
            manifest.add_step(
                "product_normalization", prompt=_PRODUCT_NORMALIZE_PROMPT,
                produced={"normalized_url": normalized_url,
                          "original_url": product_ref_url},
            )
            if on_asset:
                on_asset("product_ref_normalized", {
                    "normalized_url": normalized_url,
                    "original_url": product_ref_url,
                })
            product_ref_url = normalized_url
        else:
            on_progress(
                "  product normalization failed — using the original photo"
            )

    # 4. Per-beat anchor prompts (parallel) -------------------------------
    on_progress("writing per-beat anchor prompts...")

    async def _anchor_prompt(beat: Any) -> str:
        # Per-beat product gating — UGC anatomy: product appears in
        # solution/proof/cta, not in hook/problem (or in pure lifestyle
        # B-roll). _beat_uses_product() is the single source of truth.
        beat_has_product = _beat_uses_product(
            beat, product_ref_url, getattr(timeline, "product_name", None))
        beat_product_ref = product_ref_url if beat_has_product else None
        visual_note = _anchor_visual_note(beat)
        if beat.shot_type == "talking_head":
            return await gem.write_first_frame_prompt(
                brief=brief, character=character,
                opening_chunk_action=visual_note,
                framing=beat.framing or "selfie",
                product_in_first_frame=beat_has_product,
                product_ref_url=beat_product_ref,
            )
        broll_desc = beat.broll_shot or beat.broll_type or visual_note
        return await gem.write_broll_prompt(
            brief=brief, broll_shot=broll_desc,
            visual_note=visual_note, product_ref_url=beat_product_ref,
            character=character, broll_type=beat.broll_type,
            shows_creator=getattr(beat, "shows_creator", False),
        )

    anchor_prompts = await asyncio.gather(
        *[_anchor_prompt(b) for b in beats]
    )

    # 5. Per-beat anchor images (parallel, identity/product locked) -------
    on_progress(f"rendering {len(beats)} anchor frames (Nano Banana Pro)...")

    async def _anchor_img(beat: Any, prompt: str) -> Any:
        refs, roles = _anchor_refs(
            beat, char_ref.url, product_ref_url,
            getattr(timeline, "product_name", None))
        # All anchors use Nano Banana Pro for better identity/product
        # preservation than NB2 (small cost delta vs Seedance dominance).
        imgs = await image_client.generate_async(
            prompt=prompt, tier="hero", source="workflow",
            reference_image_urls=refs or None,
            reference_roles=roles or None,
        )
        return imgs[0]

    # Fan-out anchor renders with single-failure resilience + one retry of the
    # failed indices (covers transient TLS/API blips without doubling cost on a
    # real outage). See _gather_with_retry.
    _assert_aligned(beats=beats, anchor_prompts=anchor_prompts)
    anchors = await _gather_with_retry(
        [lambda b=b, p=p: _anchor_img(b, p)
         for b, p in zip(beats, anchor_prompts)],
        label="anchor frame(s)", on_progress=on_progress,
    )
    anchor_records = [
        {"index": i, "url": a.url,
         "shot_type": b.shot_type, "framing": b.framing,
         "broll_type": b.broll_type, "section": b.ad_section,
         "uses_product": _beat_uses_product(
             b, product_ref_url, getattr(timeline, "product_name", None))}
        for i, (a, b) in enumerate(zip(anchors, beats))
    ]
    manifest.update(anchors=anchor_records)
    for rec, prompt in zip(anchor_records, anchor_prompts):
        manifest.add_step("anchor", index=rec["index"], prompt=prompt,
                          produced={"url": rec["url"],
                                    "shot_type": rec["shot_type"],
                                    "section": rec["section"]})
    if on_asset:
        on_asset("anchors", anchor_records)

    # 5b. Per-beat motion prompts (Opus, parallel, multimodal) ------------
    on_progress("writing per-beat motion prompts (Opus)...")
    motion_records = await _write_motion_prompts(
        beats=beats, anchors=anchors, windows=windows, gem=gem,
        character=character, llm=llm, product_ref_url=product_ref_url,
        product_name=getattr(timeline, "product_name", None),
        lipsync_engine=lipsync_engine, isolated_audio=isolated_audio,
        broll_video_provider=broll_video_provider,
        pronunciations=pronunciations, omni_mode=omni_mode,
        brief_directives=(
            "\n".join(f"- {d}" for d in directives.visual_directives)
            if directives and directives.visual_directives else ""
        ),
        on_progress=on_progress,
        manifest=manifest, on_asset=on_asset,
    )
    for i in sorted(motion_records):
        rec = motion_records[i]
        manifest.add_step("motion", index=i, prompt=rec["prompt"],
                          produced={"mode": rec["mode"],
                                    "source": rec["source"],
                                    "model": rec["model"],
                                    "provider": rec["provider"],
                                    "negative_prompt": rec["negative_prompt"]})

    # 6. Per-beat clips (single-image I2V; silent, or audio-driven for
    # native-lipsync talking heads) ---------------------------------------
    on_progress(
        "generating video clips ("
        + ("Omni image-to-video, native voice baked in" if omni_mode else
           "Seedance single-image I2V"
           + ("; b-roll on Kling v3 i2v"
              if broll_video_provider == "kling" else ""))
        + ")..."
    )
    _assert_aligned(beats=beats, anchors=anchors, windows=windows)

    # Records keyed by index so a retried clip overwrites (no duplicate) and we
    # persist incrementally — each landed clip is written to the manifest the
    # instant it finishes, so a crash mid-batch never loses a paid render (the
    # clip stage previously discarded ALL billed siblings on one failure).
    clip_records: dict[int, dict] = {}
    # Task ids persisted the moment EvoLink accepts a job — BEFORE the clip
    # lands. A killed/wedged run must leave enough in the manifest to salvage
    # already-paid generations (incident 2026-07-09: 5 completed EvoLink tasks
    # were unrecoverable because ids were only written on download).
    clip_task_ids: dict[int, str] = {}

    async def _clip(i: int, beat: Any, anchor: Any,
                    window: tuple[float, float]) -> None:
        window_len = window[1] - window[0]
        # Isolated-audio engines: clip duration is derived from the per-beat
        # isolated audio (NOT the VO window). clip_dur = max(4, ceil(audio_dur))
        # so the audio always fits inside the clip (Seedance floor = 4s).
        # For silent beats the audio duration is None → use the window.
        if omni_mode:
            # Omni's API range is 3-10s — a different range from Seedance's
            # 4-15, so _requested_duration's bounds must not be used here.
            dur = _omni_duration(window_len)
        elif isolated_audio:
            audio_dur = beat_audio_durs[i]
            timing_len = audio_dur if audio_dur is not None else window_len
            dur = _requested_duration(timing_len)
        else:
            dur = _requested_duration(window_len)
        if dur > _DUR_MAX and isolated_audio:
            # Isolated mode sizes the clip to the beat's OWN per-beat TTS, not
            # the VO window. That TTS is generated independently, so it can
            # pace a line slower than the continuous VO did (audio > window) →
            # dur can exceed the soft fast-cut cap even though the window is
            # under it. Sizing to the audio is CORRECT (a shorter clip would
            # freeze/black-pad); this is informational, not a fault. Reports
            # the audio (the actual driver), not window_len.
            on_progress(
                f"  note: beat {i} isolated audio is {timing_len:.1f}s "
                f"(> {_DUR_MAX}s soft cap) — requesting {dur}s clip"
            )
        elif dur > _DUR_MAX and not omni_mode:
            # Omni is exempt: its own 10s ceiling sits above this soft fast-cut
            # cap and _omni_windows already split on that ceiling, so a 7-10s
            # omni clip is the intended shape, not a splitter miss.
            #
            # Legacy (window-driven) modes: should be unreachable after
            # split_long_windows; if it fires, a window slipped past the
            # splitter — request the longer (more expensive, slower-paced)
            # clip rather than guarantee an assembly failure after every
            # sibling has been billed.
            on_progress(
                f"  WARNING: beat {i} window is {window_len:.1f}s "
                f"(> {_DUR_MAX}s fast-cut cap) — requesting {dur}s clip"
            )
        # _beat_mode is the single source of truth: stage 5b used it to pick
        # each beat's prompt MODE, so deriving _clip's native/reaction routing
        # from the same call guarantees the audio path can never disagree with
        # the prompt Opus was handed. (A reaction beat is silent screen time —
        # never audio-driven; assemble routes it through the silent path.)
        mode = _beat_mode(beat, lipsync_engine=lipsync_engine,
                          isolated_audio=isolated_audio, omni_mode=omni_mode)
        timbre = lipsync_engine == "seedance_native"
        if omni_mode:
            # Omni authors its own speech from the prompt: reference audio is
            # unsupported (the client raises rather than silently dropping it),
            # and `native` here means "audio-DRIVEN Seedance r2v" — a different
            # thing entirely. generate_audio is set by the request builder.
            native, audio_url = False, None
        elif timbre:
            # Native-timbre engine: the shared anchor (head of the VO, uploaded
            # once above) is the reference for every speaking clip. This keeps
            # voice consistent across all beats without per-beat slice+upload
            # cost. Silent beats get no audio ref.
            native = mode in ("on_camera", "background_vo")
            audio_url = _clip_audio_url(mode, timbre_anchor_url)
        elif isolated_audio:
            # Isolated-audio engines: always silent i2v (native=False, no
            # audio_url). The per-beat isolated audio is applied in assembly
            # (lipsync for on_camera, attach_audio for background_vo,
            # attach_silence for silent). Audio must NOT be muxed into the
            # clip here.
            native = False
            audio_url = None
        else:
            # Legacy engines (direct heygen / syncso "whole"): keep the
            # original per-beat slicing path so those engines are unaffected.
            native = mode == "native_talk"
            audio_url = None
            if native:
                # Audio-driven lipsync: the beat's exact (paced) VO slice rides
                # along as Seedance reference audio. The reference MUST equal the
                # requested clip duration (pad with trailing silence) so r2v
                # rests the mouth instead of stretching/repeating the speech.
                ref = work / f"beat-{i}-ref.wav"
                slice_audio(vo_audio_path, window[0], window[1], ref)
                ref_padded = work / f"beat-{i}-ref-padded.wav"
                pad_audio_to(ref, _requested_duration(window_len), ref_padded)
                audio_url = uploader.upload(str(ref_padded))
        # Prompt was written in stage 5b (Opus, or f-string fallback) and
        # persisted to the manifest; _clip just consumes it. Provider comes off
        # the SAME record rather than being re-derived here: the prompt SHAPE is
        # provider-specific, so the body builder must follow the prompt that was
        # actually written (and a resumed run reads both back from the manifest).
        record = motion_records[i]
        prompt = record["prompt"]
        use_kling = record.get("provider") == "kling"
        # Omni uses ONE run-level seed for every clip (index 0, not i): the
        # generated VOICE is seeded too, and holding it identical across clips is
        # the only lever we have on cross-clip voice drift (there is no cloning
        # and no voice id). Seedance keeps its per-beat seed — its voice comes
        # from the reference audio, so per-beat seeds only vary the visuals.
        req = _clip_request(
            beat, anchor_url=anchor.url, duration=dur,
            seed=_seed_for(work, 0 if omni_mode else i),
            native=native, prompt=prompt,
            audio_url=audio_url, resolution=clip_resolution,
            use_kling=use_kling, omni=omni_mode,
            negative_prompt=record.get("negative_prompt"),
        )
        local = work / f"clip-{i}.mp4"

        def _persist_task_id(task_id: str) -> None:
            clip_task_ids[i] = task_id
            manifest.update(clip_task_ids={str(k): v for k, v
                                           in sorted(clip_task_ids.items())})

        # Resume (merged from af30e37): a task id persisted at creation means
        # this clip was already ordered (and billed) — hand it to _order_clip
        # so the poll/download reuses the paid EvoLink task instead of
        # re-paying. If the reused task can no longer deliver (expired
        # artifact), drop the stale id so the retry orders fresh; a FRESH
        # order keeps its id on failure (persist-at-creation, 2026-07-09).
        existing = clip_task_ids.get(i)
        if existing:
            on_progress(f"  re-using existing EvoLink task {existing} for "
                        f"clip {i + 1}/{len(beats)}")
        try:
            task_id, evolink_url, used_provider = await _order_clip(
                video_client, req, index=i, dest=local,
                on_task_id=_persist_task_id, existing_task_id=existing,
                provider=("omni" if omni_mode
                          else "kling" if use_kling else "seedance"),
                # The beat window is what survives the trim; the surplus we
                # ordered to clear the provider's duration floor is the waste.
                used_seconds=window[1] - window[0],
                fallback=_omni_fallback(req, omni_fallback_client),
                on_progress=on_progress,
            )
        except Exception:
            if existing:
                clip_task_ids.pop(i, None)
                manifest.update(clip_task_ids={str(k): v for k, v
                                               in sorted(clip_task_ids.items())})
            raise
        clip_s3_url = await _try_upload_deliverable(
            uploader, local, on_progress=on_progress)
        # The ledger bills on this number, so it must be the duration the
        # provider actually RENDERED. If Omni rejected the duration field, the
        # client retried without it and every clip of the run came back (and was
        # billed) at the 10s default — recording our 4s ask would under-report
        # the run's spend by up to 2.5x.
        # The duration quirk is Google's alone: on the EvoLink route duration is
        # an integer field that either applies or 400s the create, so a clip that
        # fell back was billed for exactly what it asked for.
        billed_dur = dur
        if (used_provider == "omni"
                and not getattr(video_client, "duration_supported", True)):
            billed_dur = OMNI_DUR_MAX
        clip_records[i] = {
            "index": i, "task_id": task_id,
            "evolink_url": evolink_url, "local_path": str(local),
            "s3_url": clip_s3_url,
            "duration_requested": billed_dur, "seed": req.seed,
            # No lipsync engine touches an omni clip (its speech is native), so
            # recording one here would misread the ledger.
            "engine": (None if omni_mode else
                       lipsync_engine if beat.shot_type == "talking_head"
                       else None),
            "audio_url": audio_url,
            # Which video family actually rendered (and billed) this clip — what
            # the per-run spend estimate below is computed from, and the reason
            # _order_clip returns it: an omni clip that fell back was billed by
            # EvoLink, at EvoLink's rate, and must not be priced as Google's.
            "provider": used_provider,
            "negative_prompt": req.negative_prompt,
        }
        # Persist the instant this (paid) clip lands. Safe to mutate shared
        # state here: asyncio is single-threaded, no preemption between the
        # assignment and the manifest write.
        manifest.update(clips=[clip_records[k] for k in sorted(clip_records)])
        manifest.add_step("clip", index=i, prompt=prompt,
                          produced={"local_path": str(local),
                                    "s3_url": clip_s3_url,
                                    "evolink_url": evolink_url})
        led = costs.active()
        so_far = f" · run cost so far ${led.total_usd:.2f}" if led else ""
        on_progress(f"  clip {i + 1}/{len(beats)} ready{so_far}")

    await _gather_with_retry(
        [lambda i=i, b=b, a=a, w=w: _clip(i, b, a, w)
         for i, (b, a, w) in enumerate(zip(beats, anchors, windows))],
        label="clip(s)", on_progress=on_progress,
    )
    clip_paths = [clip_records[i]["local_path"] for i in range(len(beats))]

    # What the clip stage just spent, broken down by provider. Clips dominate a
    # run's cost and the account is on a prepaid credit balance, so this is
    # logged (and persisted) on every run — it is also how the Kling saving is
    # verified against a Seedance-only run instead of assumed.
    #
    # This is a RATE-CARD estimate off the ordered durations, kept because the
    # resume preview must quote a price before spending one. The authoritative
    # post-run number is the ledger's, which reads EvoLink's own
    # usage.cost.usd per task.
    cost = _clip_cost_estimate(clip_records)
    manifest.update(clip_cost_estimate=cost)
    on_progress("  clip spend (rate-card estimate): " + _format_clip_spend(cost))

    # 7. Assemble + finish (shared with resume_ugc_video) -----------------
    assembled = await _finish_run(
        beats=beats, windows=windows, clip_paths=clip_paths,
        vo_audio_path=vo_audio_path, alignment=alignment,
        lipsync_client=lipsync_client, uploader=uploader, voice=voice,
        settings=settings, work=work, out_path=out_path, manifest=manifest,
        lipsync_engine=lipsync_engine, lipsync_granularity=lipsync_granularity,
        isolated_audio=isolated_audio, omni_mode=omni_mode,
        beat_audio_paths=beat_audio_paths, beat_audio_durs=beat_audio_durs,
        beat_alignments=beat_alignments,
        product_ref_url=product_ref_url,
        product_name=getattr(timeline, "product_name", None),
        clip_resolution=clip_resolution, burn_captions=burn_captions,
        finishing_grade=finishing_grade, finishing_look=finishing_look,
        color_match=color_match, visual_qc=visual_qc, room_tone=room_tone,
        transition_style=transition_style, punch_in_accent=punch_in_accent,
        on_progress=on_progress, on_asset=on_asset,
    )
    return PipelineResult(
        final_video_path=assembled.path, timeline=timeline,
        vo_audio_path=vo_audio_path, character_ref_url=char_ref.url,
        duration_s=assembled.duration_s, n_lipsynced=assembled.n_lipsynced,
    )


# ─── resume a partially-paid run ─────────────────────────────────────────────
# A killed/wedged run leaves paid EvoLink clips behind — on disk, or still
# fetchable from the task record. Resume rehydrates the manifest and resolves
# every beat's clip in TIERS, cheapest first:
#   tier 1  a complete local clip-N.mp4          → reuse, FREE
#   tier 2  a paid task whose artifact is live    → re-download, FREE
#   tier 3  nothing usable                        → re-order, PAID
# Local-first is deliberate: the three pre-914536f stranded runs have
# clip_task_ids == {} yet all their clips ON DISK, so a task-id-first resume
# would have re-ordered work that was already sitting there.

_RESUME_TIER_LABELS = {
    1: "local clip (free)",
    2: "re-downloaded from the paid task (free)",
    3: "re-ordered on EvoLink (PAID)",
}

# How long resume waits on a still-processing paid task before giving up and
# re-ordering. Shorter than the clip stage's 1200s: the task was accepted in a
# previous session, so it is either long finished or wedged.
_RESUME_POLL_DEADLINE_S = 420.0


def _resolve_run_dir(run_dir: str | Path) -> Path:
    """Accept an absolute path, a repo-relative path, or a bare run id."""
    p = Path(run_dir)
    for cand in (p, ROOT / p, WORKFLOWS_ROOT / p.name):
        if (cand / "manifest.json").is_file():
            return cand
    raise FileNotFoundError(f"no run with a manifest.json at {run_dir!r}")


def _resume_path(run: Path, recorded: str | None) -> str | None:
    """Resolve a manifest-recorded local path against reality.

    Manifests store paths exactly as the UI wrote them — repo-root-relative
    ("jobs/workflows/<run>/vo-paced.wav") — so a resume started from another cwd
    (or after the jobs tree moved) must re-anchor: as-is, then under ROOT, then
    by filename inside the run dir. Returns None when the file is gone.
    """
    if not recorded:
        return None
    for cand in (Path(recorded), ROOT / recorded, run / Path(recorded).name):
        if cand.is_file():
            return str(cand)
    return None


def _clip_candidates(run: Path, clip_rec: dict | None, i: int) -> list[Path]:
    """Where beat i's already-downloaded clip could be, best guess first.

    The canonical run-dir name wins over the manifest's recorded path: the
    recorded one is an absolute/cwd-relative string from a previous session and
    goes stale the moment the jobs tree moves.
    """
    cands = [run / f"clip-{i}.mp4"]
    recorded = (clip_rec or {}).get("local_path")
    if recorded:
        cands += [Path(recorded), ROOT / recorded, run / Path(recorded).name]
    seen: set[str] = set()
    return [c for c in cands if not (str(c) in seen or seen.add(str(c)))]


async def _task_status_once(video_client: Any,
                            task_id: str) -> tuple[str, str | None]:
    """ONE poll tick of an EvoLink task → (status, result_url | None).

    Wraps the hardened poller (whose retry/deadline logic must not be forked)
    and stops at its first tick, so a pre-commit plan never sits through a
    20-minute deadline for a task that is still processing. The generator is
    explicitly aclose()d — breaking out of an `async for` leaves an async
    generator suspended at its yield.
    """
    stream = video_client.poll_task_stream_async(task_id)
    try:
        async for status, resp in stream:
            return status, (resp.video_url if resp is not None else None)
    finally:
        await stream.aclose()
    return "unknown", None


async def _await_task_result(video_client: Any, task_id: str, *,
                             deadline_s: float) -> str | None:
    """Poll a paid task to completion → its result URL (None on any failure).

    Waiting on a task that is still running is FREE and always better than
    re-ordering it, so resume waits rather than probing once.
    """
    final = None
    async for _status, resp in video_client.poll_task_stream_async(
            task_id, max_wait_seconds=deadline_s):
        if resp is not None:
            final = resp
    return final.video_url if final is not None else None


async def _resolve_clip(
    i: int, *, run: Path, clip_rec: dict | None, task_id: str | None,
    video_client: Any, probe_only: bool = False,
    on_progress: Callable[[str], None],
) -> tuple[int, str | None]:
    """Resolve beat i's clip without spending: returns (tier, local_path|None).

    tier 3 means "nothing free is usable" — the caller re-orders it. With
    ``probe_only`` the tier is reported but nothing is downloaded (the UI's
    pre-commit breakdown), so tier 2 comes back with a None path.

    Every blocking call (ffprobe, range-GET, download) goes through
    asyncio.to_thread: this coroutine runs inside a gather, and one synchronous
    network call in there freezes every sibling (incident 2026-07-09).
    """
    for cand in _clip_candidates(run, clip_rec, i):
        if await asyncio.to_thread(_clip_is_usable, cand):
            return 1, str(cand)
    url: str | None = None
    if task_id:
        try:
            if probe_only:
                # A client that can answer "recoverable?" without transferring
                # the artifact answers it here (omni: one poll tick would
                # download the clip and re-upload it to R2 — the preview must
                # stay side-effect-free). Everyone else takes the poller's first
                # tick, which for EvoLink is a free status read that already
                # carries the result URL.
                free_probe = getattr(video_client, "probe_status_async", None)
                if free_probe is not None:
                    status, has_artifact = await free_probe(task_id)
                    if status == "completed" and has_artifact:
                        return 2, None
                else:
                    status, url = await _task_status_once(video_client, task_id)
                    if status != "completed":
                        url = None
            else:
                url = await _await_task_result(
                    video_client, task_id, deadline_s=_RESUME_POLL_DEADLINE_S)
        except Exception as exc:  # noqa: BLE001 — a dead task just means tier 3
            on_progress(f"  beat {i}: task {task_id} unusable ({exc!r})")
            url = None
    if url is None:
        # Pre-914536f runs recorded no task ids but DID record the result url of
        # every clip that landed — still worth a liveness probe.
        url = (clip_rec or {}).get("evolink_url")
    if not url:
        return 3, None
    # A "completed" task record is NOT proof the artifact exists: EvoLink keeps
    # the row long after the file expires (~72h, probed by age: 0/1/2d → 206,
    # 4d+ → 404). Decide on bytes.
    if not await asyncio.to_thread(_url_is_live, url):
        return 3, None
    if probe_only:
        return 2, None
    local = run / f"clip-{i}.mp4"
    try:
        await asyncio.to_thread(download, url, local)
    except Exception as exc:  # noqa: BLE001 — fall through to a paid re-order
        on_progress(f"  beat {i}: re-download failed ({exc!r})")
        return 3, None
    if not await asyncio.to_thread(_clip_is_usable, local):
        return 3, None
    return 2, str(local)


@dataclass
class ResumableRun:
    """One run that has paid work on disk but no final video.

    Built WITHOUT any network call (it feeds a dropdown): tiers 2 and 3 are only
    distinguishable after probing EvoLink, so `paid_task` here means "has a task
    id worth probing", not "definitely re-downloadable".
    """
    run_id: str
    run_dir: str
    status: str
    updated_at: str | None
    n_beats: int
    local: list[int] = field(default_factory=list)
    paid_task: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    blocked_reason: str | None = None

    @property
    def resumable(self) -> bool:
        return self.blocked_reason is None and self.n_beats > 0

    @property
    def label(self) -> str:
        n_paid = len(self.local) + len(self.paid_task)
        base = f"{self.run_id}  ({n_paid}/{self.n_beats} clips paid)"
        return base if self.resumable else f"{base}  [{self.blocked_reason}]"

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "run_dir": self.run_dir,
            "status": self.status, "updated_at": self.updated_at,
            "n_beats": self.n_beats, "local": self.local,
            "paid_task": self.paid_task, "missing": self.missing,
            "blocked_reason": self.blocked_reason,
            "resumable": self.resumable, "label": self.label,
        }


def resumable_runs(jobs_root: str | Path = WORKFLOWS_ROOT) -> list[ResumableRun]:
    """Runs under ``jobs_root`` that have paid clips but no final video.

    Local only (no EvoLink calls) and newest first, so the UI can refresh the
    dropdown on every click without a restart. A run whose manifest is missing
    the effective-beat geometry is still listed — with ``blocked_reason`` set —
    because silently hiding a run that holds paid clips is worse than showing
    why it can't be resumed.
    """
    root = Path(jobs_root)
    if not root.is_dir():
        return []
    out: list[ResumableRun] = []
    for mpath in sorted(root.glob("*/manifest.json")):
        run = mpath.parent
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt manifest is not resumable
            continue
        if not isinstance(m, dict):
            continue
        final = _resume_path(run, m.get("final_video_path"))
        if final or (run / "final.mp4").is_file():
            continue
        windows = m.get("beat_windows") or []
        beats = m.get("beats") or m.get("timeline", {}).get("beats") or []
        n = len(windows)
        blocked = None
        if not n:
            blocked = "died before the beat windows existed"
        elif len(beats) != n:
            # The pre-2026-07-25 manifests persisted only the PLANNER's beats;
            # split_long_windows / merge_short_windows can make the effective
            # list a different length, and there is no way to rebuild it.
            blocked = (f"manifest predates the beats record "
                       f"({len(beats)} beats vs {n} windows)")
        clips = {int(c["index"]): c for c in (m.get("clips") or [])
                 if c.get("index") is not None}
        tasks = {int(k): v for k, v in (m.get("clip_task_ids") or {}).items()}
        local, paid, missing = [], [], []
        for i in range(n):
            if any(_clip_is_usable(c) for c in _clip_candidates(run, clips.get(i), i)):
                local.append(i)
            elif tasks.get(i) or (clips.get(i) or {}).get("evolink_url"):
                paid.append(i)
            else:
                missing.append(i)
        if not local and not paid and blocked is None:
            blocked = "no paid clips to salvage"
        out.append(ResumableRun(
            run_id=run.name, run_dir=str(run),
            status=str(m.get("status") or "unknown"),
            updated_at=m.get("updated_at") or m.get("started_at"),
            n_beats=n, local=local, paid_task=paid, missing=missing,
            blocked_reason=blocked,
        ))
    # Newest first. Run ids are timestamped (v2-YYYYMMDD-HHMMSS), so the name
    # sorts chronologically and beats trusting a possibly-absent updated_at.
    return sorted(out, key=lambda r: r.run_id, reverse=True)


@dataclass
class _ResumeState:
    """Everything a resume needs, rehydrated from one run's manifest."""
    run: Path
    manifest: _Manifest
    beats: list
    windows: list[tuple[float, float]]
    alignment: Alignment
    alignment_synthetic: bool
    vo_audio_path: str
    beat_audio_paths: list[str | None]
    beat_audio_durs: list[float | None]
    beat_alignments: list[Any]
    motion: dict[int, dict]
    anchor_urls: dict[int, str]
    clip_recs: dict[int, dict]
    task_ids: dict[int, str]
    params: dict
    lipsync_engine: str
    lipsync_granularity: str
    isolated_audio: bool
    video_generator: str
    omni_mode: bool
    clip_resolution: str
    product_ref_url: str | None
    product_name: str | None
    timbre_anchor_url: str | None
    timeline: Any


def _load_resume_state(run: Path) -> _ResumeState:
    """Rehydrate a run's pipeline state from its manifest.

    Everything the clip + assembly stages consume is already persisted (the
    manifest was always written as "the resumable record"; nothing ever read it
    back — see _Manifest.load). Anything MISSING is raised here, before a client
    is built, so a resume can never half-spend and then discover it can't
    assemble.
    """
    manifest = _Manifest.load(run / "manifest.json")
    params = dict(manifest.get("params") or {})
    windows = [(float(w["start"]), float(w["end"]))
               for w in (manifest.get("beat_windows") or [])]
    if not windows:
        raise ValueError(
            f"{run.name}: no beat_windows in the manifest — the run died before "
            f"any clip was ordered; start a fresh run instead."
        )
    raw_beats = manifest.get("beats")
    if not raw_beats:
        # Fall back to the planner's beats, but ONLY when the geometry proves no
        # split/merge happened — the effective list is otherwise unrecoverable
        # (run v2-20260709-191535: 10 planner beats, 9 merged windows).
        raw_beats = (manifest.get("timeline") or {}).get("beats") or []
    if len(raw_beats) != len(windows):
        raise ValueError(
            f"{run.name}: manifest has {len(raw_beats)} beats for "
            f"{len(windows)} windows — it predates the effective-beats record "
            f"and the post-split/merge beat list cannot be rebuilt."
        )
    beats = [_rehydrate_beat(b) for b in raw_beats]
    n = len(beats)

    lipsync_engine = str(params.get("lipsync_engine") or DEFAULT_LIPSYNC_ENGINE)
    lipsync_granularity = str(
        params.get("lipsync_granularity") or SYNC_LIPSYNC_GRANULARITY)
    # Pre-omni manifests have no video_generator key — seedance by default.
    video_generator = str(params.get("video_generator") or "seedance")
    omni_mode = video_generator == "omni"
    # Same derivation as generate_ugc_video (including the omni short-circuit:
    # an omni run may carry a stale lipsync_engine param that never ran) — the
    # audio topology decides what assemble() does with every beat, so it must
    # not be re-guessed differently here.
    isolated_audio = not omni_mode and (
        lipsync_engine == "heygen_replicate"
        or (lipsync_engine == "syncso" and lipsync_granularity == "per_clip")
    )

    vo_audio_path = _resume_path(run, manifest.get("vo_audio_path"))
    if vo_audio_path is None and not isolated_audio and not omni_mode:
        # Omni runs never had a VO file to lose: the speech lives inside the
        # clips, and assemble() masters the concat's own audio.
        raise ValueError(
            f"{run.name}: the paced VO ({manifest.get('vo_audio_path')!r}) is "
            f"gone and this engine mixes the continuous VO — cannot resume."
        )

    raw_align = manifest.get("vo_alignment")
    if raw_align:
        alignment, alignment_synthetic = Alignment.from_dict(raw_align), False
    else:
        # Pre-2026-07-25 manifests kept only the DURATION — and omni manifests
        # write no vo_alignment at all (there is no VO to align). Σ windows ==
        # the recorded duration by construction, so a single-character alignment
        # reproduces every use of it except caption timing (guarded by the
        # caller; omni recovers captions by force-alignment instead).
        end = float(manifest.get("vo_duration_s") or windows[-1][1])
        alignment = Alignment(characters=["x"], starts=[0.0], ends=[end])
        alignment_synthetic = True

    beat_audio_paths: list[str | None] = [None] * n
    beat_audio_durs: list[float | None] = [None] * n
    beat_alignments: list[Any] = [None] * n
    if isolated_audio:
        recorded = manifest.get("beat_audio_paths") or []
        durs = manifest.get("beat_audio_durs") or []
        aligns = manifest.get("beat_alignments") or []
        if len(recorded) != n:
            raise ValueError(
                f"{run.name}: engine {lipsync_engine!r} needs per-beat audio but "
                f"the manifest records {len(recorded)} entries for {n} beats."
            )
        gone = []
        for i in range(n):
            if recorded[i] is None:      # a genuinely silent beat
                continue
            resolved = _resume_path(run, recorded[i])
            if resolved is None:
                gone.append(i)
                continue
            beat_audio_paths[i] = resolved
            beat_audio_durs[i] = (float(durs[i]) if i < len(durs)
                                  and durs[i] is not None else None)
            if i < len(aligns) and aligns[i]:
                beat_alignments[i] = Alignment.from_dict(aligns[i])
        if gone:
            # Re-generating the TTS would change the durations the clips were
            # SIZED to (clip_dur = max(4, ceil(audio_dur))) and invalidate the
            # recorded per-beat alignments captions ride on — so this is fatal,
            # not something to paper over with a fresh ElevenLabs call.
            raise ValueError(
                f"{run.name}: per-beat audio missing for beats {gone} — the "
                f"clips were sized to those exact takes, so re-voicing them "
                f"would desync the cut. Start a fresh run."
            )

    motion = {int(r["index"]): r for r in (manifest.get("motion_prompts") or [])
              if r.get("index") is not None}
    anchor_urls = {int(a["index"]): a["url"]
                   for a in (manifest.get("anchors") or [])
                   if a.get("index") is not None and a.get("url")}
    clip_recs = {int(c["index"]): c for c in (manifest.get("clips") or [])
                 if c.get("index") is not None}
    task_ids = {int(k): str(v)
                for k, v in (manifest.get("clip_task_ids") or {}).items() if v}

    timeline_raw = manifest.get("timeline") or {}
    try:
        from app.gemini import VideoTimeline  # noqa: PLC0415 — resume-only
        timeline: Any = VideoTimeline.model_validate(timeline_raw)
    except Exception:  # noqa: BLE001 — an older schema still resumes fine
        timeline = timeline_raw

    return _ResumeState(
        run=run, manifest=manifest, beats=beats, windows=windows,
        alignment=alignment, alignment_synthetic=alignment_synthetic,
        vo_audio_path=vo_audio_path or "",
        beat_audio_paths=beat_audio_paths, beat_audio_durs=beat_audio_durs,
        beat_alignments=beat_alignments, motion=motion,
        anchor_urls=anchor_urls, clip_recs=clip_recs, task_ids=task_ids,
        params=params, lipsync_engine=lipsync_engine,
        lipsync_granularity=lipsync_granularity, isolated_audio=isolated_audio,
        video_generator=video_generator, omni_mode=omni_mode,
        clip_resolution=str(params.get("clip_resolution")
                            or DEFAULT_CLIP_RESOLUTION),
        product_ref_url=(manifest.get("product_ref_normalized")
                         or params.get("product_ref_url")),
        product_name=(timeline_raw or {}).get("product_name"),
        timbre_anchor_url=manifest.get("timbre_anchor_url"),
        timeline=timeline,
    )


def _resume_duration(state: _ResumeState, i: int) -> int:
    """Clip seconds to order for beat i — the same rule the clip stage used
    (isolated engines size to the beat's OWN TTS, everyone else to the window),
    so a re-ordered clip is the length assemble() already expects."""
    window_len = state.windows[i][1] - state.windows[i][0]
    if state.omni_mode:
        # Omni's own 3-10s range, never _requested_duration's Seedance bounds.
        return _omni_duration(window_len)
    if state.isolated_audio:
        audio_dur = state.beat_audio_durs[i]
        return _requested_duration(
            audio_dur if audio_dur is not None else window_len)
    return _requested_duration(window_len)


def _resume_billing_geometry(state: _ResumeState, i: int) -> dict:
    """The two fields _clip_cost_estimate bills on, rebuilt from the manifest.

    The motion record is the authority on provider (stage 5b decided it, and a
    re-order must not silently change families); the duration comes from the
    same _resume_duration the original order used.
    """
    return {
        "duration_requested": _resume_duration(state, i),
        "provider": (state.motion.get(i) or {}).get("provider") or "seedance",
    }


def _resume_paid_estimate(state: _ResumeState,
                          indices: list[int]) -> dict:
    """Cost of re-ordering ``indices`` — the ONLY money a resume can spend."""
    return _clip_cost_estimate(
        {i: _resume_billing_geometry(state, i) for i in indices})


async def resume_plan(run_dir: str | Path, *, settings: Settings,
                      on_progress: Callable[[str], None] = print) -> dict:
    """Per-beat tier breakdown + the cost of resuming, WITHOUT spending it.

    This is what the UI shows before the user commits: it polls EvoLink task
    records and range-GETs their artifacts (both free) but downloads nothing and
    orders nothing. Tier 2 here is a prediction — an artifact that is live now
    can still 404 by the time resume runs, in which case that beat falls to
    tier 3 and the real cost is higher than quoted.
    """
    run = _resolve_run_dir(run_dir)
    state = _load_resume_state(run)
    # The generator that rendered the run is the only one that can answer for
    # its task ids — an EvoLink client asked about a Gemini interaction id would
    # report every beat as tier 3 and quote a full re-order.
    video_client = make_video_client(settings, state.video_generator)
    try:
        tiers = await asyncio.gather(*[
            _resolve_clip(i, run=run, clip_rec=state.clip_recs.get(i),
                          task_id=state.task_ids.get(i),
                          video_client=video_client, probe_only=True,
                          on_progress=on_progress)
            for i in range(len(state.beats))
        ])
    finally:
        await video_client.close()
    by_index = {i: t for i, (t, _) in enumerate(tiers)}
    paid = [i for i, t in by_index.items() if t == 3]
    cost = _resume_paid_estimate(state, paid)
    blockers = [i for i in paid
                if i not in state.anchor_urls or i not in state.motion]
    return {
        "run_id": run.name,
        "run_dir": str(run),
        "n_beats": len(state.beats),
        "tiers": by_index,
        "free": [i for i, t in by_index.items() if t in (1, 2)],
        "paid": paid,
        "paid_cost": cost,
        "paid_usd": cost["total_usd"],
        "lipsync_engine": state.lipsync_engine,
        # A tier-3 beat with no anchor image / no motion prompt cannot be
        # re-ordered at all — surface it before the user commits.
        "unrecoverable": blockers,
        "summary": _resume_plan_summary(run.name, by_index, cost, blockers),
    }


def _resume_plan_summary(run_id: str, tiers: dict[int, int], cost: dict,
                         blockers: list[int]) -> str:
    """Human-readable plan, one line per beat + the total PAID cost."""
    lines = [f"{run_id}: {len(tiers)} beats"]
    for i in sorted(tiers):
        lines.append(f"  beat {i}: {_RESUME_TIER_LABELS[tiers[i]]}")
    n_paid = sum(1 for t in tiers.values() if t == 3)
    if n_paid:
        lines.append(f"  → re-orders {n_paid} clip(s): "
                     + _format_clip_spend(cost))
    else:
        lines.append("  → nothing to re-order: this resume costs $0")
    if blockers:
        lines.append(f"  !! beats {blockers} have no anchor/motion prompt to "
                     f"re-order from — resume will fail")
    return "\n".join(lines)


async def resume_ugc_video(
    run_dir: str | Path, *, settings: Settings,
    out_path: str | Path | None = None,
    allow_paid_reorder: bool = True,
    burn_captions: bool | None = None,
    finishing_grade: bool = FINISHING_GRADE,
    finishing_look: str = FINISHING_LOOK,
    color_match: bool = COLOR_MATCH_CLIPS,
    visual_qc: bool = VISUAL_QC,
    room_tone: bool = ROOM_TONE,
    transition_style: str = TRANSITION_STYLE,
    punch_in_accent: bool = PUNCH_IN_ACCENT,
    on_progress: Callable[[str], None] = print,
    on_asset: Callable[[str, Any], None] | None = None,
) -> PipelineResult:
    """Finish a partially-paid run: reuse every paid clip, re-order only gaps.

    Rehydrates the run's manifest, resolves each beat's clip in tiers (local →
    live paid task → re-order) and then continues into the SAME lipsync +
    assembly tail as a fresh run (_finish_run), which reuses already-paid
    lipsync results via manifest["lipsync"].

    Costs nothing but the re-orders: no planner, no TTS, no anchor re-render —
    every prompt/anchor/audio artefact is read back from the manifest. With
    ``allow_paid_reorder=False`` a run that needs a re-order raises instead of
    spending (the "$0 only" path).

    Idempotent: a second call finds every clip locally (tier 1), re-uses the
    recorded lipsync results, and just re-renders the assembly — $0.
    """
    # A resume's ledger covers only what THIS pass spends, which is the number
    # worth seeing ("what did resuming cost me"). _finish_run appends it to the
    # manifest's cost_passes rather than replacing the original run's record,
    # so the directory still knows what it cost in total.
    costs.start_run()
    run = _resolve_run_dir(run_dir)
    state = _load_resume_state(run)
    manifest = state.manifest
    n = len(state.beats)
    # Default output = the run's own recorded name, inside the run dir. The
    # recorded string itself is a previous session's cwd-relative path and the
    # file does not exist yet (that's why we're resuming), so only the FILENAME
    # is reusable — re-anchoring by name keeps the deliverable with its run.
    recorded_out = manifest.get("out_path")
    out = Path(out_path) if out_path else (
        run / (Path(recorded_out).name if recorded_out else "final.mp4"))
    engine_label = (
        "omni (native voice, no lipsync pass)" if state.omni_mode
        else f"{state.lipsync_engine}/{state.lipsync_granularity}")
    on_progress(
        f"resuming {run.name}: {n} beats, engine={engine_label} → {out}")
    manifest.mark("resuming")

    uploader = make_uploader(settings)
    video_client = make_video_client(settings, state.video_generator)
    # No lipsync pass exists in omni mode (the speech is inside the clips), so
    # don't demand that engine's keys to finish an omni run.
    lipsync_client = (None if state.omni_mode
                      else make_lipsync_client(settings, state.lipsync_engine))
    # Only for its force_align capability in _finish_run (native captions) —
    # resume never generates speech. Pinned to ElevenLabs regardless of the
    # configured provider: alignment is always ElevenLabs, and this way a resume
    # never needs a Cartesia/Fish key it will not call.
    # Omni mirrors generate_ugc_video: a blank key is legal there (captions just
    # skip), so demanding one here would make an already-paid omni run
    # unrecoverable — _finish_run's only use is getattr(voice, "force_align").
    voice = (None if state.omni_mode and not settings.elevenlabs_api_key
             else make_voice_client(settings, "elevenlabs"))

    tiers: dict[int, int] = {}
    resolved: dict[int, str] = {}
    try:
        # Phase A — the FREE tiers, concurrently. Every blocking call inside is
        # on a worker thread (see _resolve_clip).
        free = await asyncio.gather(*[
            _resolve_clip(i, run=run, clip_rec=state.clip_recs.get(i),
                          task_id=state.task_ids.get(i),
                          video_client=video_client, on_progress=on_progress)
            for i in range(n)
        ])
        for i, (tier, path) in enumerate(free):
            tiers[i] = tier
            if path:
                resolved[i] = path
            if tier == 2:
                # Record where the re-downloaded artifact now lives (merging, so
                # the original record's cost/seed/provider survive) — a second
                # resume must find it as a tier-1 local clip. When the first run
                # died BEFORE writing the clip record there is nothing to merge,
                # so the billing geometry is rebuilt from the motion record
                # first: without it _clip_cost_estimate prices this already-paid
                # clip at 0s under seedance, silently under-reporting the run's
                # spend ledger. The spread comes after, so a real record wins.
                state.clip_recs[i] = {
                    "index": i, **_resume_billing_geometry(state, i),
                    **state.clip_recs.get(i, {}),
                    "local_path": path, "redownloaded": True,
                }
            on_progress(f"  beat {i}: {_RESUME_TIER_LABELS[tier]}")

        # Phase B — the gaps. Cost is logged BEFORE anything is ordered.
        todo = [i for i in range(n) if i not in resolved]
        paid_cost = _resume_paid_estimate(state, todo)
        if todo:
            missing_inputs = [i for i in todo
                              if i not in state.anchor_urls or i not in state.motion]
            if missing_inputs:
                raise ValueError(
                    f"{run.name}: beats {missing_inputs} have no anchor image "
                    f"and/or motion prompt in the manifest — the run died "
                    f"before stage 5b, so they cannot be re-ordered."
                )
            on_progress(
                f"  re-ordering {len(todo)} clip(s) {todo}: "
                + _format_clip_spend(paid_cost)
            )
            if not allow_paid_reorder:
                raise RuntimeError(
                    f"{run.name}: {len(todo)} clip(s) would have to be "
                    f"re-ordered (${paid_cost['total_usd']:.2f}) but "
                    f"allow_paid_reorder=False."
                )
            # One fallback client for the whole re-order fan-out, same as the
            # fresh clip stage — and built here because this is where `settings`
            # lives. omni_mode is per-CLIP on resume (a legacy manifest can mix
            # providers), so arm it whenever the run itself was an omni run and
            # let _omni_fallback decide per clip.
            reorder_fallback_client = _make_omni_fallback_client(
                settings, state.omni_mode, on_progress=on_progress)
            await _gather_with_retry(
                [lambda i=i: _resume_reorder_clip(
                    i, state=state, video_client=video_client,
                    uploader=uploader, dest=run / f"clip-{i}.mp4",
                    omni_fallback_client=reorder_fallback_client,
                    on_progress=on_progress)
                 for i in todo],
                label="re-ordered clip(s)", on_progress=on_progress,
            )
            for i in todo:
                resolved[i] = str(run / f"clip-{i}.mp4")
                tiers[i] = 3
        else:
            on_progress("  every clip was already paid for — this resume is $0")
    finally:
        await video_client.close()

    reordered = [i for i in sorted(tiers) if tiers[i] == 3]
    cost = _resume_paid_estimate(state, reordered)
    manifest.update(
        clips=[state.clip_recs[k] for k in sorted(state.clip_recs)],
        resume={
            "at": datetime.now(timezone.utc).isoformat(),
            "out_path": str(out),
            "tiers": {str(i): tiers[i] for i in sorted(tiers)},
            "reused_local": [i for i in sorted(tiers) if tiers[i] == 1],
            "redownloaded": [i for i in sorted(tiers) if tiers[i] == 2],
            "reordered": reordered,
            "paid_cost": cost,
        },
    )
    on_progress(
        f"  resume spend: {len(reordered)} re-ordered clip(s) → "
        + (_format_clip_spend(cost) if reordered else "$0.00")
    )

    # Captions on a continuous-VO engine need the real character-level
    # alignment; a legacy manifest only kept the duration, and word-popping a
    # synthetic one-character alignment would burn garbage over the master.
    # Native mode is exempt — it force-aligns the script against the audio, and
    # so does omni (whose manifests never carry an alignment by design).
    burn = BURN_CAPTIONS if burn_captions is None else burn_captions
    if (burn and state.alignment_synthetic and not state.isolated_audio
            and not state.omni_mode
            and state.lipsync_engine != "seedance_native"):
        on_progress(
            "  captions disabled: this manifest predates the persisted VO "
            "alignment, so word timing cannot be recovered"
        )
        burn = False

    assembled = await _finish_run(
        beats=state.beats, windows=state.windows,
        clip_paths=[resolved[i] for i in range(n)],
        vo_audio_path=state.vo_audio_path, alignment=state.alignment,
        lipsync_client=lipsync_client, uploader=uploader, voice=voice,
        settings=settings, work=run, out_path=out, manifest=manifest,
        lipsync_engine=state.lipsync_engine,
        lipsync_granularity=state.lipsync_granularity,
        isolated_audio=state.isolated_audio, omni_mode=state.omni_mode,
        beat_audio_paths=state.beat_audio_paths,
        beat_audio_durs=state.beat_audio_durs,
        beat_alignments=state.beat_alignments,
        product_ref_url=state.product_ref_url,
        product_name=state.product_name,
        clip_resolution=state.clip_resolution, burn_captions=burn,
        finishing_grade=finishing_grade, finishing_look=finishing_look,
        color_match=color_match, visual_qc=visual_qc, room_tone=room_tone,
        transition_style=transition_style, punch_in_accent=punch_in_accent,
        on_progress=on_progress, on_asset=on_asset,
    )
    return PipelineResult(
        final_video_path=assembled.path, timeline=state.timeline,
        vo_audio_path=state.vo_audio_path,
        character_ref_url=manifest.get("character_ref_url") or "",
        duration_s=assembled.duration_s, n_lipsynced=assembled.n_lipsynced,
    )


async def _resume_reorder_clip(
    i: int, *, state: _ResumeState, video_client: Any, uploader: Any,
    dest: Path, on_progress: Callable[[str], None],
    omni_fallback_client: Any = None,
) -> None:
    """Tier 3: re-order ONE beat's clip on EvoLink (the only paid step).

    Rebuilds the request from the manifest — same anchor, same prompt, same
    provider, same deterministic seed (derived from the run dir, so a re-order
    reproduces the original request) — and goes through _order_clip, so the task
    id is persisted on acceptance exactly as in the clip stage.
    """
    beat = state.beats[i]
    record = state.motion[i]
    omni = record.get("provider") == "omni"
    mode = _beat_mode(beat, lipsync_engine=state.lipsync_engine,
                      isolated_audio=state.isolated_audio, omni_mode=omni)
    dur = _resume_duration(state, i)
    if omni:
        # Omni re-order: the recorded prompt already carries the voice persona
        # and the line, so the only thing to reproduce is the request shape —
        # no reference audio, no negative prompt, and the RUN-LEVEL seed
        # (index 0) every omni clip of this run was ordered with.
        native, audio_url = False, None
    elif state.lipsync_engine == "seedance_native":
        native = mode in ("on_camera", "background_vo")
        audio_url = _clip_audio_url(mode, state.timbre_anchor_url)
        if native and not audio_url:
            raise ValueError(
                f"{state.run.name}: beat {i} is an audio-driven native clip but "
                f"the manifest has no timbre_anchor_url to drive it."
            )
    elif state.isolated_audio:
        # Isolated engines render silent clips; the per-beat audio is applied in
        # assembly, never muxed into the clip.
        native, audio_url = False, None
    else:
        native = mode == "native_talk"
        audio_url = None
        if native:
            # Legacy continuous-VO engines: the beat's padded VO slice rides
            # along as Seedance reference audio. Local ffmpeg + one S3 put —
            # no provider spend. Off the event loop: this runs inside a gather.
            ref = state.run / f"beat-{i}-ref.wav"
            ref_padded = state.run / f"beat-{i}-ref-padded.wav"
            await asyncio.to_thread(
                slice_audio, state.vo_audio_path,
                state.windows[i][0], state.windows[i][1], ref)
            await asyncio.to_thread(pad_audio_to, ref, dur, ref_padded)
            audio_url = await asyncio.to_thread(uploader.upload, str(ref_padded))
    use_kling = record.get("provider") == "kling"
    req = _clip_request(
        beat, anchor_url=state.anchor_urls[i], duration=dur,
        seed=_seed_for(state.run, 0 if omni else i),
        native=native, prompt=record["prompt"],
        audio_url=audio_url, resolution=state.clip_resolution,
        use_kling=use_kling, omni=omni,
        negative_prompt=record.get("negative_prompt"),
    )

    def _persist_task_id(task_id: str) -> None:
        state.task_ids[i] = task_id
        state.manifest.update(clip_task_ids={
            str(k): v for k, v in sorted(state.task_ids.items())})

    task_id, evolink_url, used_provider = await _order_clip(
        video_client, req, index=i, dest=dest, on_task_id=_persist_task_id,
        provider="omni" if omni else "kling" if use_kling else "seedance",
        used_seconds=state.windows[i][1] - state.windows[i][0],
        fallback=_omni_fallback(req, omni_fallback_client if omni else None),
        on_progress=on_progress)
    clip_s3_url = await _try_upload_deliverable(
        uploader, dest, on_progress=on_progress)
    # Same ledger rule as the fresh path: if Omni rejected the duration field,
    # the client retried without it and the clip was billed at the 10s default.
    billed_dur = (OMNI_DUR_MAX
                  if used_provider == "omni"
                  and not getattr(video_client, "duration_supported", True)
                  else dur)
    state.clip_recs[i] = {
        "index": i, "task_id": task_id, "evolink_url": evolink_url,
        "local_path": str(dest), "s3_url": clip_s3_url,
        "duration_requested": billed_dur, "seed": req.seed,
        "engine": (None if omni else
                   state.lipsync_engine if beat.shot_type == "talking_head"
                   else None),
        "audio_url": audio_url,
        "provider": used_provider,
        "negative_prompt": req.negative_prompt,
        "resumed": True,
    }
    state.manifest.update(
        clips=[state.clip_recs[k] for k in sorted(state.clip_recs)])
    state.manifest.add_step("clip", index=i, prompt=record["prompt"],
                            produced={"local_path": str(dest),
                                      "s3_url": clip_s3_url,
                                      "evolink_url": evolink_url,
                                      "resumed": True})
    on_progress(f"  beat {i}: re-ordered clip ready")
