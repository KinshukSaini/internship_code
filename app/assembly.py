"""Final-cut assembly: beat clips → lipsync → concat → VO overlay.

Timing model (the crux):
  - The full VO is one continuous track; its character timestamps are the
    master clock.
  - Each timeline beat maps to a [start,end] window via beat_windows(); every
    window boundary sits on the 24fps frame grid, so each clip's -t encodes
    an exact whole number of frames and Σ clip lengths == VO duration with no
    per-beat quantization drift.
  - Each beat's video clip is generated at ceil(window) seconds (Seedance
    only does integer durations). B-roll is TRIMMED to the exact window
    length; talking_head clips go to Sync.so at FULL length against a VO
    slice covering the whole clip's span — so the model paces the mouth over
    the complete take, never a truncated stub — and the synced output is then
    trimmed to the window in one re-encode.
  - The lipsynced audio is stripped — we overlay the FULL continuous VO once
    at the end (explicit -t, never -shortest) so the voice is gapless and the
    mouth (synced to a slice that sits at exactly that position in the final
    timeline) lines up. B-roll clips stay silent (VO plays over them). Drift
    between the concat and the VO warns above 0.05s and raises above 0.25s.

Native-lipsync mode (audio-driven reference-to-video) inverts the audio
strategy: r2v RE-RENDERS the reference audio (same words/voice, ~±0.3s of
internal re-timing), so overlaying the continuous VO would visibly offset the
mouth and a hard window trim can chop the re-timed last word. Instead every
clip carries its OWN uniform aac track — talking heads keep their baked
speech, trimmed to the detected end of speech (never under the window, capped
at the source); B-roll gets its beat's VO slice attached — and the concat IS
the final mix. Per-clip audio makes global drift structurally impossible, so
the drift check only logs, and durations intentionally deviate from the
windows by up to ~0.4s/beat.

Lipsync runs CONCURRENTLY: each Sync.so call is ~2 min, so N talking-head
beats used to cost N×2min serially. They now fan out via asyncio.gather, so
wall-clock collapses to ~the slowest single beat.

Finishing: after the concat/overlay, the master is finished in exactly TWO
steps — master_audio() (room tone + fade + two-pass loudnorm to −14 LUFS /
−1 dBTP) and finish(), the single CRF18 video re-encode that applies the
phone-capture grade (grade before grain, deliberately), burns the
word-pop captions (last, so text stays crisp), tags BT.709 and muxes the
mastered audio at 192k AAC. Caption timing is mode-aware: continuous-VO modes
use the global alignment; isolated-audio mode shifts each beat's OWN TTS
alignment by its clip's concat offset (the continuous alignment does NOT
match that timeline); native mode skips captions (r2v re-times speech, no
trustworthy alignment exists).
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.grading import finishing_vf, match_corrections, measure_clip_stats
from app.captions import (
    FONTS_DIR,
    Word,
    build_ass,
    ffmpeg_has_subtitles_filter,
    subtitles_filter,
    words_for_beats,
    words_from_alignment,
)
from app.stitcher import download, stitch
from app.voice import Alignment

# Uniform encode params so the concat demuxer can stream-copy. A single run
# encodes EVERY clip at one resolution (stream-copy needs identical dims), so
# the resolution is chosen once and threaded into every encode call.
_W, _H = 720, 1280   # default (720p), kept as the fallback dims
# 9:16 vertical dims per Seedance `quality` value. All even (yuv420p).
_RES_DIMS: dict[str, tuple[int, int]] = {
    "480p": (480, 854),
    "720p": (720, 1280),
    "1080p": (1080, 1920),
}
# x264 quality: 16 is visually transparent / near-archival (the user asked for
# the highest quality sync.so render; the dominant lever is resolution, this is
# the no-downscale companion). crf is encode-time only — it never breaks the
# concat demuxer's stream-copy (which keys on codec/dims/pixfmt/timebase).
_CRF = "16"


def _dims(resolution: str | None) -> tuple[int, int]:
    return _RES_DIMS.get(resolution or "720p", (_W, _H))


_FPS = 24
_FRAME = 1.0 / _FPS


# eleven_v3 audio tags ([excited], [laughs], …) are spoken-instruction tokens:
# they shape delivery but are NOT in the spoken output, so they're absent from
# the character-level alignment. We must strip them from dialogue slices before
# locating them in the alignment, or every window after a tag drifts.
_AUDIO_TAG_RE = re.compile(r"\[[^\]]*\]")


def _strip_tags(text: str) -> str:
    return _AUDIO_TAG_RE.sub("", text)


def _is_reaction(beat: Any) -> bool:
    """A reaction beat is silent screen time (no speech). It is NEVER lipsynced
    or native-r2v — it routes through the silent trim path like B-roll in both
    engines (the continuous VO / per-clip slice already covers its hold). Read
    defensively: older beats carry no beat_kind."""
    return getattr(beat, "beat_kind", "speech") == "reaction"


def beat_windows(beats: list[Any], alignment: Alignment,
                 full_script: str | None = None) -> list[tuple[float, float]]:
    """Map each beat's dialogue_text to a [start,end] time window.

    ``alignment.characters`` is the ground-truth spoken sequence, indexed 1:1
    with the timing arrays. It DOES still contain the [audio tag] characters —
    measured 2026-07-29: sending "…me. [worried] Every wash…" returns that exact
    string, with '[worried]' spanning 2.920-3.447s (0.527s of real time). An
    earlier version of this docstring claimed the API consumes the tags; it does
    not, so every tag — not only audible ones like [sighs] — occupies characters
    that carry duration.
    That is harmless here because we search each beat's TAG-STRIPPED dialogue
    slice inside that string: the tag characters sit immediately before the match
    (planner contract), so find() still lands and the tag's time is absorbed by
    the start-snap below. Verified contiguous at 3 tags / 6 beats.
    Falls back to a running cursor when a slice isn't found.

    The returned windows are a CONTIGUOUS partition of [0, vo_duration]:
    window i starts exactly where window i-1 ends, the first starts at 0 and
    the last ends at the VO's end — so Σ window lengths == VO duration and the
    assembled video can never drift against the overlaid VO. Every interior
    boundary sits on the 24fps frame grid (the final end stays the VO's true,
    unrounded end) so each clip's -t encodes a whole number of frames.
    """
    starts, ends = alignment.starts, alignment.ends
    n = len(starts)
    if n == 0:
        return [(0.0, 0.4) for _ in beats]

    # The alignment's own characters are 1:1 with the timing arrays, so prefer
    # them over full_script. (Both carry the tag characters — see the docstring;
    # the per-beat search below is what makes tags harmless, not their absence.)
    align_text = "".join(alignment.characters) if alignment.characters \
        else (_strip_tags(full_script) if full_script else "")

    windows: list[tuple[float, float]] = []
    cursor = 0
    for b in beats:
        dialogue = _strip_tags(b.dialogue_text or "")
        length = len(dialogue)
        c0 = cursor
        if align_text and dialogue:
            # Forward-only resync: locate the slice at/after the running cursor.
            # We deliberately do NOT fall back to an unanchored global find() —
            # a repeated short slice ("so good", "I know") could otherwise bind
            # to an EARLIER occurrence and drift every later cut backward. If
            # the forward search misses, keep c0 = cursor (advance by length),
            # which stays monotonic.
            idx = align_text.find(dialogue, cursor)
            if idx != -1:
                c0 = idx
        c0 = min(max(c0, 0), n - 1)
        c1 = min(c0 + max(length, 1) - 1, n - 1)
        c1 = max(c1, c0)
        start = float(starts[c0])
        end = float(ends[c1])
        if end <= start:                      # guard zero/negative windows
            end = start + 0.4
        windows.append((start, end))
        cursor = c0 + length

    # Enforce a CONTIGUOUS partition of the full VO timeline. Audible audio-tag
    # sounds ([sighs], [laughs]) have NO characters in the alignment, so the
    # search above leaves their audio in no window — and any hole breaks the
    # assembly invariant Σ window lengths == VO duration (every visual after
    # the hole runs ahead of the audio, and -shortest chops the tail; a real
    # run measured a 0.92s [sighs] hole desyncing 6 of 9 beats). Tags sit
    # immediately before the words they modify (planner contract), so the tag
    # audio belongs to the FOLLOWING beat: snap each window's start to the
    # previous window's end, and extend the last window to the VO's end.
    # Interior boundaries snap to the nearest WORD boundary (a space char's end
    # time) before the grid: the planner partitions full_script but is NOT
    # forced to cut between words, so a raw boundary can fall INSIDE a word and
    # split its audio across two beats. In native mode the two halves come from
    # different sources (a talking head bakes its own re-rendered speech; its
    # B-roll neighbour gets a [start,end] VO slice), so the word is heard
    # clipped at the cut. Snapping to the word gap keeps each beat's audio on
    # whole words (no usable space leaves the raw boundary).
    # Interior boundaries also snap to the 24fps frame grid— -t quantizes
    # every clip to whole frames, so an off-grid boundary leaks a sub-frame
    # remainder per beat and the concat drifts against the VO. The final end
    # is exempt (it must reach the VO's true end); two frames is the minimum
    # non-degenerate window on the grid.
    # Inter-word silence spans [space_start, space_end]; either edge is a valid
    # word gap. Including both means an already word-aligned boundary (ending
    # on the last char of a word, i.e. a space's start) stays put, while a
    # mid-word boundary snaps to the nearest gap.
    gap_times = sorted(
        {float(starts[i]) for i, ch in enumerate(alignment.characters)
         if ch == " "}
        | {float(ends[i]) for i, ch in enumerate(alignment.characters)
           if ch == " "}
    )
    fixed: list[tuple[float, float]] = []
    prev_end = 0.0
    for i, (start, end) in enumerate(windows):
        start = prev_end
        if i < len(windows) - 1:
            if gap_times:
                cands = [t for t in gap_times if t > start + _FRAME]
                if cands:
                    end = min(cands, key=lambda t: abs(t - end))
            end = round(end * _FPS) / _FPS
        end = max(end, start + 2 * _FRAME)
        fixed.append((start, end))
        prev_end = end
    if fixed:
        s_last, e_last = fixed[-1]
        fixed[-1] = (s_last, max(e_last, float(ends[-1])))
    return fixed


def split_long_windows(
    beats: list[Any], windows: list[tuple[float, float]],
    alignment: Alignment, *, max_s: float = 5.0, target_s: float = 3.0,
) -> tuple[list[Any], list[tuple[float, float]]]:
    """Deterministically split any window longer than ``max_s`` into equal
    sub-windows of ≤ ``target_s``, cut at word boundaries.

    This is the runaway SAFETY NET, not the pacing driver. The director defaults
    to fast 2-3s cuts and gets ONE held beat up to ~4-5s (the plan-time word
    caps enforce this) — those are kept whole. Only a window that overruns the
    ~5s ceiling (eleven_v3 pacing varies 1.6-2.2 w/s, so even a capped beat can
    speak past it) gets chopped here into ~3s jump-cut sub-beats — the
    user-set fast-cut bar (2026-07-31; was 8.0/7.0, which let a 6s shot sit
    "stuck" on screen). Each sub-beat is a shallow copy of its parent (same
    framing/visual/product gating) with its own anchor + clip downstream —
    i.e. a jump cut.

    Cut points are the ideal equal divisions snapped to the nearest word
    boundary (a space character's end time) inside the window, then to the
    24fps frame grid (window boundaries must stay frame-aligned end to end),
    so a lipsync slice never starts mid-word. Falls back to the ideal point
    when a window has no usable space (cuts only affect visuals; the VO is
    continuous).

    Every emitted beat carries ``sub_index``: 0 for unsplit beats, j (0..k-1)
    for the j-th sub-beat of a split — downstream uses it to differentiate
    anchors between the sub-beats of one parent.
    """
    # End time of every word boundary (space char) in the spoken sequence.
    space_times = [
        float(alignment.ends[i])
        for i, ch in enumerate(alignment.characters) if ch == " "
    ]
    out_beats: list[Any] = []
    out_wins: list[tuple[float, float]] = []
    sub_indices: list[int] = []
    for b, (start, end) in zip(beats, windows):
        length = end - start
        if length <= max_s:
            out_beats.append(copy.copy(b))
            out_wins.append((start, end))
            sub_indices.append(0)
            continue
        k = math.ceil(length / target_s)
        cuts: list[float] = []
        prev = start
        for j in range(1, k):
            ideal = start + length * j / k
            candidates = [t for t in space_times if prev + 0.5 < t < end - 0.5]
            cut = (min(candidates, key=lambda t: abs(t - ideal))
                   if candidates else ideal)
            cut = round(cut * _FPS) / _FPS               # frame grid
            cut = min(max(cut, prev + 0.2), end - 0.2)   # monotonic + in-range
            cuts.append(cut)
            prev = cut
        bounds = [start, *cuts, end]
        # Split the dialogue into k contiguous word-chunks, one per sub-beat.
        # Continuous-VO engines slice the shared VO by window and ignore a
        # sub-beat's dialogue_text, but engines that re-speak dialogue_text per
        # clip (heygen_replicate isolated TTS, seedance_native prompt) would
        # otherwise re-utter the WHOLE line in every sub-clip. (The
        # "concatenate dialogue_text → full_script" check is a planner-output
        # contract that runs BEFORE this split, so chunking here is safe.)
        words = (getattr(b, "dialogue_text", "") or "").split()
        for j in range(k):
            sub = copy.copy(b)
            if words:
                chunk = " ".join(words[j * len(words) // k:(j + 1) * len(words) // k])
                try:
                    object.__setattr__(sub, "dialogue_text", chunk)
                except Exception:
                    pass
            out_beats.append(sub)
            out_wins.append((bounds[j], bounds[j + 1]))
            sub_indices.append(j)
    # Reindex sequentially — indices are used for logging/manifest records;
    # sub_index marks a beat's position within its parent's jump-cut run.
    for i, (b, j) in enumerate(zip(out_beats, sub_indices)):
        try:
            object.__setattr__(b, "index", i)
            object.__setattr__(b, "sub_index", j)
        except Exception:
            pass
    return out_beats, out_wins


def merge_short_windows(
    beats: list[Any], windows: list[tuple[float, float]],
    *, min_s: float = 2.2,
) -> tuple[list[Any], list[tuple[float, float]]]:
    """Merge any talking_head window shorter than ``min_s`` into the PREVIOUS
    window; a short FIRST window merges into the next instead. Merging
    REPEATS until no talking_head window is under ``min_s`` (or only one
    window remains): a single pass can leave a still-short merge — e.g. a
    0.4s B-roll absorbing a 1.5s talking head — and a sub-2s reference slice
    is a hard EvoLink 400 *after* every sibling clip was billed.

    A talking-head take under ~2s can't land a readable lipsync beat — the
    cut plays as a stutter. The merged window is the combined span of both
    originals; the beat whose window was longer keeps its fields (ties go to
    the earlier beat) so the dominant shot's framing/visual survives.
    Non-talking_head windows are never merged — B-roll is allowed to be a
    fast insert. Pure function: inputs are untouched; the returned beats are
    copies, reindexed like split_long_windows().
    """
    out_beats: list[Any] = [copy.copy(b) for b in beats]
    out_wins: list[tuple[float, float]] = list(windows)
    merged = True
    while merged and len(out_wins) > 1:
        merged = False
        for i, (b, (start, end)) in enumerate(zip(out_beats, out_wins)):
            # Reaction beats are intentional SILENT holds, not lipsync takes —
            # never merge them away (their real span is minted later by
            # apply_holds; merging here would discard an authored/brief-mandated
            # moment, as it did to the lingering-scent beat in v2-20260614-001634).
            if (b.shot_type != "talking_head" or _is_reaction(b)
                    or (end - start) >= min_s):
                continue
            lo, hi = (i, i + 1) if i == 0 else (i - 1, i)
            len_lo = out_wins[lo][1] - out_wins[lo][0]
            len_hi = out_wins[hi][1] - out_wins[hi][0]
            keep = out_beats[lo] if len_lo >= len_hi else out_beats[hi]
            out_wins[lo:hi + 1] = [(out_wins[lo][0], out_wins[hi][1])]
            out_beats[lo:hi + 1] = [copy.copy(keep)]
            merged = True
            break
    # Reindex sequentially — indices are used for logging/manifest records.
    for i, b in enumerate(out_beats):
        try:
            object.__setattr__(b, "index", i)
        except Exception:
            pass
    return out_beats, out_wins


def _insert_silences(
    vo_path: str | Path, insertions: list[tuple[float, float]],
    dest: str | Path,
) -> None:
    """Splice silence INTO the VO in one ffmpeg pass (mirror of trim_silences'
    cut pass, inverted): each ``(at, dur)`` opens a ``dur``-second gap at the
    pre-offset time ``at`` in the source. The source is split at every
    insertion point into kept segments, silence segments are minted from
    anullsrc, and all are concatenated in order — pcm wav, so the already-lossy
    mp3 VO is never re-encoded through mp3 a second time (downstream consumers
    accept wav, same as trim_silences)."""
    cuts = sorted(at for at, _ in insertions)
    parts: list[str] = []
    labels: list[str] = []
    # Kept segments = the source split at each insertion point; asetpts resets
    # each segment's clock so concat is gapless (same idiom as trim_silences).
    prev = 0.0
    for i, c in enumerate(cuts):
        spec = f"atrim=start={prev:.3f}:end={c:.3f}" if c > prev \
            else f"atrim=start={prev:.3f}:end={prev:.3f}"
        parts.append(f"[0:a]{spec},asetpts=PTS-STARTPTS[k{i}]")
        labels.append(f"[k{i}]")
        prev = c
    parts.append(f"[0:a]atrim=start={prev:.3f},asetpts=PTS-STARTPTS[k{len(cuts)}]")
    # Silence segments minted at the source rate so concat sees uniform audio.
    for j, (at, dur) in enumerate(sorted(insertions)):
        parts.append(
            f"anullsrc=r=44100:cl=mono,atrim=duration={dur:.3f},"
            f"asetpts=PTS-STARTPTS[s{j}]"
        )
    # Interleave: kept[0], silence[0], kept[1], silence[1], …, kept[n].
    order: list[str] = []
    for i in range(len(cuts)):
        order.append(f"[k{i}]")
        order.append(f"[s{i}]")
    order.append(f"[k{len(cuts)}]")
    n = len(order)
    parts.append(f"{''.join(order)}concat=n={n}:v=0:a=1[out]")

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(vo_path),
         "-filter_complex", ";".join(parts), "-map", "[out]",
         "-c:a", "pcm_s16le", "-ar", "44100", str(dest)],
        check=True, capture_output=True,
    )


def apply_holds(
    beats: list[Any], windows: list[tuple[float, float]],
    alignment: Alignment, vo_audio_path: str | Path, *, work_dir: str | Path,
) -> tuple[str, Alignment, list[tuple[float, float]]]:
    """Open silent reaction holds in the VO timeline and shift everything after.

    Runs AFTER beat_windows/split/merge (windows already cover the speech
    beats; reaction beats got a degenerate placeholder). Walks the beats with a
    cumulative ``offset`` (= silence inserted so far):
      - a SPEECH beat keeps its window, shifted by +offset;
      - a REACTION beat (beat_kind=="reaction", hold_s>0) becomes
        [prev_end, prev_end + hold_s] and records a silence INSERTION of
        ``hold_s`` at the pre-offset time (the previous speech end), then
        offset += hold_s.
    The recorded silences are spliced into the VO in ONE ffmpeg pass (mirror of
    pacing.trim_silences — pcm wav, no mp3 re-encode), and the alignment is
    rebuilt by ADDING the cumulative hold to every char time at/after each
    insertion point (mirror of pacing._remap, inverted), so new duration ==
    old + Σhold_s. No-op (inputs returned untouched, no ffmpeg pass) when no
    reaction beat is present."""
    reaction_idx = [
        i for i, b in enumerate(beats)
        if getattr(b, "beat_kind", "speech") == "reaction"
        and (getattr(b, "hold_s", None) or 0.0) > 0.0
    ]
    if not reaction_idx:
        return str(vo_audio_path), alignment, list(windows)

    new_windows: list[tuple[float, float]] = []
    insertions: list[tuple[float, float]] = []   # (pre-offset time, hold_s)
    offset = 0.0
    prev_end = 0.0
    for i, (b, (start, end)) in enumerate(zip(beats, windows)):
        if (getattr(b, "beat_kind", "speech") == "reaction"
                and (getattr(b, "hold_s", None) or 0.0) > 0.0):
            hold = float(b.hold_s)
            # The hold is silent screen time spliced at the previous speech end
            # (pre-offset clock); the window sits at the shifted end.
            insertions.append((prev_end, hold))
            new_windows.append((prev_end + offset, prev_end + offset + hold))
            offset += hold
        else:
            new_windows.append((start + offset, end + offset))
            prev_end = end

    out = Path(work_dir) / "vo-holds.wav"
    _insert_silences(vo_audio_path, insertions, out)

    # Shift every char time by the cumulative hold inserted before it (mirror
    # of _remap, ADD instead of subtract). The splice cuts the source at `at`
    # with atrim's half-open [.,at)/[at,.) intervals, so a time EXACTLY at an
    # insertion point lands on whichever side it bounds: a char START at `at`
    # opens the post-silence segment (shift), a char END at `at` closes the
    # pre-silence segment (stay). Hence starts use >= and ends use >.
    ins_sorted = sorted(insertions)

    def _shift(t: float, *, inclusive: bool) -> float:
        added = 0.0
        for at, dur in ins_sorted:
            if t > at or (inclusive and t == at):
                added += dur
            else:
                break
        return t + added

    new_alignment = Alignment(
        characters=list(alignment.characters),
        starts=[_shift(t, inclusive=True) for t in alignment.starts],
        ends=[_shift(t, inclusive=False) for t in alignment.ends],
    )
    return str(out), new_alignment, new_windows


def _run(cmd: list[str]) -> None:
    # capture_output + re-raise WITH the stderr tail: a bare
    # CalledProcessError hides ffmpeg's actual complaint, which turned a
    # missing-libass build into an undebuggable "exit status 234" at the very
    # end of a paid run (2026-08-01).
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "replace")[-2000:]
        raise RuntimeError(
            f"{cmd[0]} failed (exit {exc.returncode}): ...{tail}"
        ) from exc


def _probe_duration(path: str | Path) -> float:
    """Return a media file's duration in seconds via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _probe_audio_streams(path: str | Path) -> dict:
    """Audio stream count + first stream's duration (native-lipsync clips
    must arrive with the voice baked in — record what actually landed)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    lines = [ln for ln in out.stdout.strip().splitlines() if ln]
    try:
        duration = float(lines[0]) if lines else None
    except ValueError:                        # ffprobe prints N/A for some muxes
        duration = None
    return {"audio_streams": len(lines), "audio_duration_s": duration}


def _has_audio_stream(path: str | Path) -> bool:
    """Best-effort "does this clip carry a track?" — False when probing fails."""
    try:
        return (_probe_audio_streams(path).get("audio_streams") or 0) > 0
    except Exception:                          # noqa: BLE001 — probe is advisory
        return False


# Native-lipsync trim: r2v re-renders the reference audio, so speech can sit
# ~±0.3s off its VO position inside the clip — the trim must reach the ACTUAL
# end of speech plus a small breath, never the window alone.
_SILENCEDETECT_FILTER = "silencedetect=noise=-35dB:d=0.15"
_SPEECH_TAIL_PAD_S = 0.12

# Tail fade on every native speaking clip. BytePlus documents that r2v narration
# clips are prone to an abrupt click / cut-off noise at the end, and prescribe a
# volume envelope on the ending audio as the fix. finish() fades only the FINAL
# master, so without this each mid-timeline native boundary can click. Kept well
# under _SPEECH_TAIL_PAD_S's breath so it never eats the last word.
_NATIVE_TAIL_FADE_S = float(os.environ.get("NATIVE_TAIL_FADE_S", "0.08"))
# Isolated-audio talking heads are generated at max(4, ceil(speech)) — the
# Seedance 4s floor + integer ceil, so a short line leaves up to ~1.7s of
# clip where the mouth still animates but no audio plays (dead-air pacing +
# lips-moving-without-sound). After lipsync we trim each speaking beat back to
# its real speech end + this natural micro-beat, never keeping that tail. A
# touch longer than the native _SPEECH_TAIL_PAD_S so fast cuts still breathe.
_ISOLATED_TAIL_PAD_S = 0.30


def _isolated_trim(audio_path: str | Path,
                   clip_dur: float) -> tuple[float, float]:
    """(final length, tail fade) for an isolated-audio speaking beat.

    Length: trim to the real speech end (+ a natural tail), capped at the
    generated clip length so we never trim past what the source clip actually
    contains. Falls back to the full clip_dur when speech-end detection fails
    (never SHORTER than the speech — the cap keeps it inside the clip).

    Fade: the click guard belongs in the silence AFTER the speech, never on top
    of the last consonant. When the cap eats the pad — audio that fills its whole
    generated clip, e.g. a 4.95s line in a 5s clip — the fade shrinks to whatever
    headroom is left (0 = no fade) instead of ramping real words down."""
    speech_end = _detect_speech_end(audio_path)
    if speech_end is None:
        return clip_dur, _NATIVE_TAIL_FADE_S
    trim_len = min(clip_dur, speech_end + _ISOLATED_TAIL_PAD_S)
    return trim_len, max(0.0, min(_NATIVE_TAIL_FADE_S, trim_len - speech_end))

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


def _detect_speech_end(path: str | Path) -> float | None:
    """End of speech in a clip's baked audio track.

    silencedetect (~-35dB, 0.15s min) lists silence runs; speech ends where a
    TRAILING run starts (one that extends to the end of the audio — a dangling
    silence_start, or a silence_end at EOF), else at the audio's full
    duration. Best-effort: returns None when probing fails, and the caller
    falls back to the window length.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", str(path), "-af", _SILENCEDETECT_FILTER,
             "-f", "null", "-"],
            check=True, capture_output=True, text=True,
        )
        audio_dur = _probe_audio_streams(path).get("audio_duration_s")
        if audio_dur is None:
            audio_dur = _probe_duration(path)
        starts = [float(v) for v in _SILENCE_START_RE.findall(proc.stderr)]
        ends = [float(v) for v in _SILENCE_END_RE.findall(proc.stderr)]
        if starts and (len(ends) < len(starts)
                       or ends[-1] >= audio_dur - 0.1):
            return max(0.0, starts[-1])
        return float(audio_dur)
    except Exception:
        return None


def _fmt_t(duration: float) -> str:
    """Format a duration for ffmpeg -t. ffmpeg keeps a frame iff pts < t, so
    rounding half-up to the millisecond can land just past an exact frame
    boundary (k/24 for k ≡ 1 mod 3 is x.41(6) → ".417") and capture one EXTRA
    frame per clip. Truncating to the millisecond below never crosses a
    boundary (1ms ≪ one 24fps frame), so on-grid windows encode exactly N
    frames (verified empirically: -t 0.417 → 11 frames, -t 0.416 → 10)."""
    return f"{math.floor(duration * 1000) / 1000:.3f}"


def trim_clip(src: str | Path, duration: float, dest: str | Path,
              resolution: str | None = None,
              extra_vf: str | None = None) -> None:
    """Trim to exact `duration`, strip audio, normalise encode + dimensions.

    ``extra_vf`` appends a per-beat accent (transition head-pop / product
    punch-in) after the uniform scale/crop — inside the same re-encode, so
    accents never add an encode generation or change the clip's duration."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    w, h = _dims(resolution)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h}")
    if extra_vf:
        vf += f",{extra_vf}"
    _run([
        "ffmpeg", "-y", "-i", str(src), "-t", _fmt_t(duration), "-an",
        "-vf", vf,
        "-c:v", "libx264", "-crf", _CRF, "-preset", "medium",
        "-pix_fmt", "yuv420p", "-r", "24", str(dest),
    ])


def trim_keep_audio(src: str | Path, duration: float, dest: str | Path,
                    resolution: str | None = None,
                    extra_vf: str | None = None,
                    tail_fade_s: float = _NATIVE_TAIL_FADE_S) -> None:
    """Trim to exact `duration` KEEPING the clip's own audio (native-lipsync
    talking heads arrive with the re-rendered speech baked in). Video params
    match trim_clip; the audio normalises to the uniform aac 44100 stereo
    every native-mode clip carries so the concat demuxer can stream-copy.

    A short tail fade rides this same re-encode: BytePlus documents that r2v
    narration clips end with an abrupt click / cut-off noise, and prescribe a
    volume envelope as the fix. The finishing pass only fades the final master,
    so without this every mid-timeline native boundary can click. Skipped when
    the clip is shorter than the fade (nothing sensible to ramp).
    """
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    w, h = _dims(resolution)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h}")
    if extra_vf:
        vf += f",{extra_vf}"
    cmd = [
        "ffmpeg", "-y", "-i", str(src), "-t", _fmt_t(duration),
        "-vf", vf,
        "-c:v", "libx264", "-crf", _CRF, "-preset", "medium",
        "-pix_fmt", "yuv420p", "-r", "24",
    ]
    if tail_fade_s > 0 and duration > tail_fade_s:
        st = duration - tail_fade_s
        cmd += ["-af", f"afade=t=out:st={st:.3f}:d={tail_fade_s:.3f}"]
    cmd += ["-c:a", "aac", "-ar", "44100", "-ac", "2", str(dest)]
    _run(cmd)


def strip_audio(src: str | Path, dest: str | Path,
                duration: float | None = None,
                resolution: str | None = None,
                extra_vf: str | None = None) -> None:
    """Re-encode video-only to uniform params (e.g. after Sync.so lipsync).

    Sync.so's output duration is never trusted: pass ``duration`` to trim to
    the beat window in the same re-encode, keeping Σ clips == VO duration.
    ``resolution`` keeps the run's dims so a 1080p sync.so output is NOT
    downscaled here (the whole point of the high-quality path).
    """
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    w, h = _dims(resolution)
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if duration is not None:
        cmd += ["-t", _fmt_t(duration)]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h}")
    if extra_vf:
        vf += f",{extra_vf}"
    cmd += [
        "-an",
        "-vf", vf,
        "-c:v", "libx264", "-crf", _CRF, "-preset", "medium",
        "-pix_fmt", "yuv420p", "-r", "24", str(dest),
    ]
    _run(cmd)


# The "real phone capture" grade, applied inside the single finishing encode.
# Order matters: grade (contrast/saturation) BEFORE grain — grading through
# grain shifts the texture — and grain must be TEMPORAL (allf=t+u) or it
# reads as a dirty lens instead of a live sensor. Clean AI renders lack this
# signature and the ABSENCE is itself an AI tell; the grain also dithers away
# banding the contrast lift creates on flat AI gradients (skies, walls).
# End-of-ad fade lengths (video slightly shorter so sound outlasts picture by
# a hair — an abrupt joint stop reads as a cut-off upload).
_VIDEO_FADE_S = 0.3
_AUDIO_FADE_S = 0.4


def head_accent_vf(style: str, resolution: str | None = None) -> str:
    """Per-beat cut-accent filter for the first frames of an incoming clip.

    UGC uses hard cuts — crossfades read corporate AND consume overlap time,
    desyncing the exactly-matched audio. The accent instead animates the
    clip's own head INSIDE its trim re-encode (duration preserved):
      punch — enters at 106% zoom, settles to 100% over 5 frames;
      slide — enters half a frame-width right, settles centred over 3 frames.
    Frame-expression crops (n = frame index) with a scale back to the uniform
    dims so the concat demuxer's stream-copy invariants hold.
    """
    w, h = _dims(resolution)
    if style == "punch":
        z = "(1.06-0.012*min(n,5))"
        return f"crop=w='iw/{z}':h='ih/{z}',scale={w}:{h}"
    if style == "slide":
        return (
            f"scale={round(w * 1.15 / 2) * 2}:{round(h * 1.15 / 2) * 2},"
            f"crop=w={w}:h={h}:"
            f"x='(iw-ow)/2+(iw-ow)/2*max(0,1-n/3)':y='(ih-oh)/2'"
        )
    return ""


def punch_in_vf(resolution: str | None = None) -> str:
    """Static 12% tighter framing for the product-reveal beat — the hard cut
    INTO the punch-in is the reveal accent."""
    w, h = _dims(resolution)
    return f"crop=iw/1.12:ih/1.12,scale={w}:{h}"


def _parse_loudnorm_stats(stderr: str) -> dict:
    """Extract the JSON stats block loudnorm prints at the end of pass 1."""
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
    if not m:
        raise RuntimeError(
            f"loudnorm pass 1 printed no stats JSON — stderr tail: "
            f"{stderr[-500:]}"
        )
    return json.loads(m.group(0))


# Dialogue-correction chain applied to the voice track BEFORE the room-tone
# mix and loudness normalization. loudnorm only sets the LEVEL; perceived
# harshness/"loudness" of TTS speech comes from spectral balance and peaks,
# which this chain treats (values per docs/production-polish research):
#   highpass 100Hz — rumble/plosive cut (male VO wants 80-100, female
#                    100-120; 100 is the safe middle for a mixed voice pool)
#   deesser        — TTS sibilance ("s" spikes read loud); s=o keeps output
#   EQ             — the TTS "processed" tells: -3dB low-mid mud @300Hz,
#                    +2.5dB presence @4kHz (phone-speaker intelligibility),
#                    +2dB air @11kHz
#   acompressor    — gentle evenness: thr 0.1 (≈−20dB, LINEAR not dB!),
#                    2.5:1, fast attack, makeup 1.41 (+3dB)
# NO limiter: loudnorm's own 100ms look-ahead true-peak limiter goes last.
_DIALOGUE_CHAIN = (
    "highpass=f=100,"
    "deesser=i=0.4:m=0.5:f=0.5:s=o,"
    "equalizer=f=300:t=q:w=1.2:g=-3,"
    "equalizer=f=4000:t=q:w=1.0:g=2.5,"
    "equalizer=f=11000:t=q:w=0.8:g=2,"
    "acompressor=threshold=0.1:ratio=2.5:attack=5:release=120:knee=6:"
    "makeup=1.41"
)


def master_audio(src: str | Path, dest: str | Path, *, duration: float,
                 room_tone: bool = True, i: float = -14.0, tp: float = -1.0,
                 lra: float = 11.0) -> None:
    """Master the final audio: dialogue correction + room tone + tail fade +
    two-pass loudnorm.

    ``src`` is whatever carries the final mix (the VO wav in overlay mode,
    the concatenated video in baked-audio modes). Two passes because one-pass
    loudnorm is dynamic (it pumps); pass 1 measures, pass 2 applies the
    measured values with ``linear=true`` (a single clean gain). Room tone is
    faint pink noise (~−49 dBFS) under the whole timeline so inter-beat gaps
    never hit digital silence — a dropout-like tell. loudnorm resamples to
    192kHz internally, so the output is pinned back to 48kHz.
    """
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    fade_start = max(0.0, duration - _AUDIO_FADE_S)
    inputs = ["-i", str(src)]
    # NOTE deliberately NO loudnorm dual_mono: the master is always delivered
    # upmixed to stereo (-ac 2), so the plain stereo measurement is the one
    # platforms see — dual_mono would land the delivered file 3 LU under
    # target (verified empirically in test_master_audio_normalizes_to_target_lufs).
    if room_tone:
        inputs += ["-f", "lavfi", "-i",
                   f"anoisesrc=color=pink:amplitude=0.0035:d={duration:.3f}"]
        pre = (f"[0:a]{_DIALOGUE_CHAIN}[dlg];"
               "[dlg][1:a]amix=inputs=2:duration=first:normalize=0,"
               f"afade=t=out:st={fade_start:.3f}:d={_AUDIO_FADE_S}")
    else:
        pre = (f"[0:a]{_DIALOGUE_CHAIN},"
               f"afade=t=out:st={fade_start:.3f}:d={_AUDIO_FADE_S}")
    spec = f"I={i:g}:TP={tp:g}:LRA={lra:g}"
    measure = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", *inputs,
         "-filter_complex", f"{pre},loudnorm={spec}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    stats = _parse_loudnorm_stats(measure.stderr)
    apply_ln = (
        f"{spec}:measured_I={stats['input_i']}:"
        f"measured_TP={stats['input_tp']}:measured_LRA={stats['input_lra']}:"
        f"measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:linear=true"
    )
    _run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", f"{pre},loudnorm={apply_ln},aresample=48000",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        "-t", f"{duration:.3f}", str(dest),
    ])


def finish(video: str | Path, mastered_audio: str | Path, dest: str | Path,
           *, duration: float, ass_path: str | Path | None = None,
           grade: bool = True, look: str = "clean_bright") -> None:
    """The SINGLE finishing re-encode: grade + grain + captions + BT.709 +
    mastered audio, one CRF18 pass on the assembled master.

    Filter order is deliberate: the selected look then grain
    (grading.finishing_vf), THEN captions — so text is burned crisp on top,
    never grained — then the tail fade over everything (captions fade with
    the picture). BT.709 tags
    stop platform transcoders guessing the matrix and shifting colours.
    ``-tune grain`` only when grain exists, so the encoder doesn't waste bits
    preserving noise that isn't there.
    """
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    if ass_path is not None and not ffmpeg_has_subtitles_filter():
        # Fail OPEN: by this point every clip has been paid for and lipsynced.
        # A caption burn that cannot work on this ffmpeg build (no libass —
        # the 2026 homebrew formula dropped it) must degrade to a caption-less
        # master, never kill the run at its final step (2026-08-01 e2e run).
        print(
            "[finish] WARNING: this ffmpeg build lacks the `subtitles` filter "
            "(built without libass) — captions SKIPPED. Install a "
            "libass-capable ffmpeg and set FFMPEG_DIR to burn captions.",
            flush=True,
        )
        ass_path = None
    vf_parts = []
    if grade:
        vf_parts.append(finishing_vf(look))
    if ass_path is not None:
        # Copy fonts into the job dir so fontsdir is a relative path with no
        # Windows drive letter — an absolute path like C:\... can never be
        # embedded in a -vf filter string because ':' is the option separator.
        import shutil
        local_fonts = Path(dest).parent / "fonts"
        local_fonts.mkdir(parents=True, exist_ok=True)
        for f in FONTS_DIR.iterdir():
            dst_f = local_fonts / f.name
            if not dst_f.exists():
                shutil.copy2(str(f), str(dst_f))
        vf_parts.append(subtitles_filter(ass_path, fonts_dir=local_fonts))
    fade_start = max(0.0, duration - _VIDEO_FADE_S)
    vf_parts.append(f"fade=t=out:st={fade_start:.3f}:d={_VIDEO_FADE_S}")
    # setparams (not the -colorspace/-color_trc output flags, which only set
    # colorspace on ffmpeg 8) stamps all three BT.709 fields into the VUI so
    # platform transcoders don't guess the matrix and shift colours.
    vf_parts.append(
        "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(video), "-i", str(mastered_audio),
        "-map", "0:v", "-map", "1:a",
        "-vf", ",".join(vf_parts),
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
    ]
    if grade:
        cmd += ["-tune", "grain"]
    cmd += [
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-t", f"{duration:.3f}", str(dest),
    ]
    _run(cmd)


def slice_audio(src: str | Path, start: float, end: float, dest: str | Path) -> None:
    """Cut [start,end] from the VO into a wav (for lipsync input).

    -ss sits AFTER -i (output-seek): input-seeking an mp3 snaps to a frame
    boundary up to ~26ms early, bleeding the previous beat's tail into the
    audio the mouth is synced against.
    """
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-i", str(src), "-ss", f"{start:.3f}",
        "-t", f"{max(0.05, end - start):.3f}", "-c:a", "pcm_s16le",
        "-ar", "44100", str(dest),
    ])


def attach_audio(video: str | Path, audio: str | Path, dest: str | Path,
                 duration: float) -> None:
    """Give an already-trimmed silent B-roll clip its beat's VO slice as the
    clip's OWN audio track (native mode has no global VO overlay). Video is
    stream-copied untouched; the audio matches the uniform aac 44100 stereo
    of the native talking heads so the concat demuxer can stream-copy."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-i", str(video), "-i", str(audio),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        "-t", f"{duration:.3f}", str(dest),
    ])


def attach_silence(video: str | Path, dest: str | Path, duration: float,
                   resolution: str | None = None) -> None:
    """Mux a synthetic silent audio track onto a video clip (native-mode
    silent beats: reaction holds and b-roll with no dialogue). The track is
    generated with anullsrc normalised to aac 44100 stereo — matching the
    uniform codec every native-mode clip carries — so the concat demuxer can
    stream-copy the full timeline without a re-encode. Video is stream-copied
    untouched at its source resolution; the `resolution` argument is accepted
    for call-site uniformity with trim_clip/trim_keep_audio but not applied
    (no rescale happens here). -t caps both streams at the exact window length."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-i", str(video),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        "-t", f"{duration:.3f}", str(dest),
    ])


def pad_audio_to(src: str | Path, target_s: float, dest: str | Path) -> None:
    """Pad ``src`` with TRAILING silence to exactly ``target_s`` (pcm wav).

    A native talking-head reference MUST equal the requested clip duration:
    r2v stops stretching/repeating the speech when the reference audio already
    fills the clip's length, resting the mouth through the trailing silence
    instead (validated in scripts/probe_native_silence.py). apad=pad_dur adds
    the silence and -t caps the output at the exact target.
    """
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-i", str(src),
        "-af", f"apad=pad_dur={target_s:.3f}", "-t", f"{target_s:.3f}",
        "-c:a", "pcm_s16le", "-ar", "44100", str(dest),
    ])


@dataclass(frozen=True)
class AssembledVideo:
    path: str
    duration_s: float
    n_beats: int
    n_lipsynced: int


async def _caption_words(*, alignment: Alignment,
                         beat_alignments: list[Alignment | None] | None,
                         ordered: list[Path], native_lipsync: bool,
                         isolated_audio: bool, beats: list[Any] | None = None,
                         final_video: str | Path | None = None,
                         force_align: Any = None) -> list[Word] | None:
    """Words + timings for the caption burn, resolved per audio mode.

    Continuous-VO modes (overlay path, whole-video sync) use the global
    alignment as-is. Isolated mode's timeline is per-beat TTS concatenated
    back-to-back, so each beat's own alignment is shifted by its clip's
    concat offset. Native mode has NO trustworthy TTS alignment (r2v
    re-renders speech with ~±0.3s of re-timing), so the KNOWN script text is
    force-aligned against the audio that actually plays in the assembled
    video (``force_align`` — ElevenLabs Forced Alignment) and captions ride
    that true clock. Alignment failure skips captions (mistimed text is
    worse than none) but never fails the run.
    """
    if native_lipsync:
        if force_align is None or final_video is None or not beats:
            print("[assemble] captions skipped: native mode needs a "
                  "force_align client to recover word timing from the "
                  "re-rendered speech.", file=sys.stderr, flush=True)
            return None
        # Same authority as the trim routing: what the planner says is spoken.
        from app.pipeline import _strip_tags, resolve_voice_mode  # noqa: PLC0415
        spoken = " ".join(
            _strip_tags(b.dialogue_text or "").strip()
            for b in beats
            if resolve_voice_mode(b) in ("on_camera", "background_vo")
            and (b.dialogue_text or "").strip()
        ).strip()
        if not spoken:
            return None
        try:
            aligned = await force_align(media_path=final_video, text=spoken)
        except Exception as exc:  # noqa: BLE001 — captions are best-effort
            print(f"[assemble] captions skipped: forced alignment failed "
                  f"({type(exc).__name__}: {exc}).", file=sys.stderr,
                  flush=True)
            return None
        return words_from_alignment(aligned)
    if not isolated_audio:
        return words_from_alignment(alignment)
    if beat_alignments is None:
        print("[assemble] captions skipped: isolated-audio mode needs "
              "beat_alignments (per-beat TTS word timing) — none supplied.",
              file=sys.stderr, flush=True)
        return None
    offsets: list[float] = []
    t = 0.0
    for clip in ordered:
        offsets.append(t)
        try:
            d = _probe_duration(clip)
        except Exception:
            d = None
        if d is None:
            print(f"[assemble] captions skipped: could not probe {clip} for "
                  f"concat offsets — mistimed captions are worse than none.",
                  file=sys.stderr, flush=True)
            return None
        t += d
    return words_for_beats(beat_alignments, offsets)


async def assemble(
    *, beats: list[Any], windows: list[tuple[float, float]],
    clip_paths: list[str], vo_audio_path: str, alignment: Alignment,
    lipsync_client: Any, uploader: Any, work_dir: str | Path,
    out_path: str | Path,
    on_asset: Any = None, manifest: Any = None,
    burn_captions: bool = False,
    native_lipsync: bool = False,
    native_ambient_audio: bool = False,
    isolated_audio: bool = False,
    beat_audio_paths: list[str | None] | None = None,
    beat_audio_durs: list[float | None] | None = None,
    whole_video_lipsync: bool = False,
    lipsync_concurrency: int = 1,
    prior_lipsync_records: list[dict] | None = None,
    clip_resolution: str = "720p",
    finishing_grade: bool = True,
    room_tone: bool = True,
    transition_style: str = "cut",
    punch_in_beat: int | None = None,
    beat_alignments: list[Alignment | None] | None = None,
    caption_words_per_line: int = 3,
    emphasis_terms: tuple[str, ...] = (),
    force_align: Any = None,
    loudnorm_i: float = -14.0,
    finishing_look: str = "clean_bright",
    color_match: bool = False,
) -> AssembledVideo:
    """(lipsync talking_head at FULL length, in parallel) + trim B-roll →
    concat → overlay VO.

    clip_paths[i] is the locally-downloaded Seedance clip for beat i (silent,
    ceil-duration). lipsync_client.lipsync(video_url, audio_url) and
    uploader.upload(local_path) are injected (real or fake).

    talking_head beats upload the ORIGINAL full clip plus a VO slice covering
    the clip's span (sync_mode=silence pads when the VO runs out before the
    video), then trim the synced output to the beat window in ONE re-encode
    (strip_audio with duration). B-roll trims locally via trim_clip.

    With ``native_lipsync=True`` talking heads arrive already lipsynced with
    the RE-RENDERED speech baked in (r2v re-times it ~±0.3s inside the clip),
    so they skip Sync.so and keep their own audio: the trim runs to the
    detected end of speech (+ a breath, never under the window, capped at the
    source) via trim_keep_audio, recorded with the audio probe into
    manifest["native_audio"]; a clip that arrives with NO audio stream raises
    (the concat would ship a mute mouth). B-roll trims silent as usual and
    then gets its
    beat's VO slice attached as the clip's own track.

    ``native_ambient_audio=True`` additionally says the NON-speaking clips came
    back with their own ambience (an end-to-end generator that authors audio for
    every clip, not just the speaking ones), so those beats keep their track
    instead of getting the synthetic silent one. Audio-less i2v clips leave it
    False — there is nothing to keep. The concat of those
    per-clip tracks IS the final — no continuous-VO overlay, and the drift
    check only logs (durations intentionally exceed the windows).

    With ``isolated_audio=True`` (heygen_replicate engine, or syncso in
    per_clip granularity) each speaking beat carries its OWN isolated TTS
    (beat_audio_paths/beat_audio_durs, index-aligned with beats): on_camera
    beats are lipsynced against their padded per-beat audio (muxed onto the
    clip first only for clients with ``video_needs_audio``), background_vo
    beats get the audio attached locally, silent beats get a synthetic silent
    track. The synced/attached audio IS the final mix — no continuous-VO
    overlay, so cross-clip A/V drift is structurally impossible.

    With ``whole_video_lipsync=True`` (syncso only; mutually exclusive with
    native) EVERY beat is trimmed silently and concatenated first, then the whole
    silent cut + the full VO go to Sync.so in ONE call. The synced output already
    carries the VO, so there is no per-clip overlay and ``n_lipsynced`` is 1.
    EXPERIMENTAL: b-roll/reaction beats are also driven by the audio.

    Lipsync calls fan out concurrently via asyncio.gather. Each successful
    lipsync persists its Sync.so URL to manifest["lipsync"] the moment it
    finishes — so a crash mid-batch never loses an already-paid-for call.
    return_exceptions=True means one failed beat won't cancel the others'
    in-flight work; the failure is surfaced only after all beats settle.

    Finishing (all modes): the assembled master goes through master_audio()
    (room tone + fade + two-pass loudnorm) and finish() (grade/grain +
    caption burn + BT.709, one CRF18 re-encode). ``burn_captions`` burns
    word-pop captions with mode-aware timing (isolated mode shifts each
    beat's own ``beat_alignments`` entry by its clip's concat offset; native
    mode recovers timing via ``force_align``). ``transition_style``
    adds a duration-preserving motion accent on incoming b-roll heads;
    ``punch_in_beat`` frames that beat 12% tighter (product-reveal accent).
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    n = len(beats)
    final_clips: list[Path | None] = [None] * n
    lipsync_records: list[dict] = []
    native_audio_records: list[dict] = []

    # 1. Local prep (serial ffmpeg/ffprobe — fast, and it fails BEFORE any
    # paid Sync.so call). B-roll is cut to its exact window here; native
    # talking heads are cut to the detected end of speech (keeping their own
    # audio); lipsync-path talking heads stay FULL length so Sync.so paces
    # the mouth across the whole take instead of a truncated stub.
    # Shot matching: measure every SOURCE clip once, correct toward the
    # median inside each clip's existing re-encode (trim/strip) — zero extra
    # encode generations. Unmeasurable clips get no correction.
    corrections: list[str] = [""] * n
    if color_match:
        corrections = match_corrections(
            [measure_clip_stats(c) for c in clip_paths]
        )
        n_corrected = sum(1 for c in corrections if c)
        if n_corrected:
            print(f"[assemble] shot matching: correcting {n_corrected}/{n} "
                  f"clips toward the median exposure/cast",
                  file=sys.stderr, flush=True)

    src_durs: list[float | None] = []
    for i, ((start, end), clip) in enumerate(zip(windows, clip_paths)):
        window_len = end - start
        # Trim-safety: a source clip SHORTER than what we trim it to makes
        # ffmpeg's -t silently freeze / black-pad. Catch it as a clear error
        # instead of shipping a frozen beat. The trim target is engine-specific:
        # isolated-audio engines (heygen_replicate / syncso per_clip) pace each
        # beat off its ISOLATED per-beat audio (see the isolated branch below —
        # clip_trim_len = min(15, b_dur)), NOT the VO window, so such a clip is
        # intentionally shorter than its window whenever the beat's audio is
        # shorter than the window. Validating it against window_len would
        # reject a correctly-sized clip. Match the downstream trim length per
        # engine. (Best-effort: skip if ffprobe fails.)
        if isolated_audio:
            _b_dur = (beat_audio_durs or [None] * n)[i]
            required_len = (min(15.0, float(_b_dur)) if _b_dur is not None
                            else window_len)
        else:
            required_len = window_len
        try:
            src_dur: float | None = _probe_duration(clip)
        except Exception:
            src_dur = None
        src_durs.append(src_dur)
        if src_dur is not None and src_dur + 0.05 < required_len:
            raise RuntimeError(
                f"beat {i}: source clip is {src_dur:.2f}s but the beat needs "
                f"{required_len:.2f}s — trimming would freeze/black-pad. The clip "
                f"was generated too short for its beat (check Seedance duration)."
            )
        # A reaction beat is silent screen time — never lipsync/native-r2v.
        # It always trims silently (the continuous VO / its own silent slice
        # covers the hold), so fall through to the silent trim path below.
        is_reaction = _is_reaction(beats[i])
        talking_head = beats[i].shot_type == "talking_head" and not is_reaction
        # In native mode, route by whether the beat speaks rather than by shot
        # type: on_camera (talking_head) AND background_vo b-roll both arrive
        # with the re-rendered speech baked in, so both must keep their own
        # audio. Only truly silent beats (reaction holds, b-roll with no
        # dialogue) need a synthetic silent track.
        # Use the same authority as the pipeline: resolve_voice_mode honours an
        # explicit voice_mode field set by the planner, preventing pipeline /
        # assembly divergence when voice_mode contradicts dialogue_text.
        # Imported lazily inside assemble() to avoid the circular import —
        # pipeline imports from assembly at module top, so a top-level import
        # here would form a cycle.
        from app.pipeline import resolve_voice_mode  # noqa: PLC0415
        voice_mode = resolve_voice_mode(beats[i])
        speaking = voice_mode in ("on_camera", "background_vo")
        # Legacy per-clip mode (direct heygen) keeps talking heads FULL length
        # (they're lipsynced individually below). Native, isolated-audio, and
        # whole-video modes trim them here: native keeps its own re-rendered
        # audio; isolated-audio trims on_camera beats full-length (lipsynced
        # below) and trims background_vo/silent with per-beat audio;
        # whole-video trims silently for the single downstream pass.
        if (talking_head and not native_lipsync and not whole_video_lipsync
                and not (isolated_audio and voice_mode != "on_camera")):
            # legacy per-clip talking heads → deferred to section 2.
            # isolated-audio on_camera talking heads → deferred to section 2.
            # All other isolated-audio beats (background_vo / silent /
            # reaction) → fall through to be trimmed here.
            if not isolated_audio or voice_mode == "on_camera":
                continue
        dst = work / f"beat-{i}-trim.mp4"
        # Cut accent, applied inside the beat's trim re-encode (duration
        # preserved): the product-reveal beat holds a 12% tighter framing;
        # other incoming b-roll heads get the transition pop. Talking heads
        # and the opening beat stay untouched (nothing to cut from, and the
        # settle must never fight a synced mouth).
        accent: str | None = None
        if beats[i].shot_type == "broll":
            if i == punch_in_beat:
                accent = punch_in_vf(clip_resolution)
            elif i > 0 and transition_style in ("punch", "slide"):
                accent = head_accent_vf(transition_style, clip_resolution)
        # Colour correction rides the same re-encode, before the accent crop.
        accent = ",".join(x for x in (corrections[i], accent or "") if x) or None
        if native_lipsync and speaking:
            # Speaking beats (on_camera talking heads AND background_vo b-roll
            # with dialogue) arrive with their r2v-rendered speech baked in.
            # Speech-end trim: the re-rendered last word can land past the
            # window; cutting at the window would chop it. Never under the
            # window (the beat must fill its slot), capped at the source.
            speech_end = _detect_speech_end(clip)
            trim_end = (window_len if speech_end is None
                        else max(window_len, speech_end + _SPEECH_TAIL_PAD_S))
            if src_dur is not None:
                trim_end = min(src_dur, trim_end)
            rec = {"index": i, "source_local_path": str(clip),
                   "speech_end": speech_end, "trim_end": trim_end}
            try:
                rec.update(_probe_audio_streams(clip))
            except Exception:
                pass
            native_audio_records.append(rec)
            if manifest is not None:
                manifest.update(native_audio=list(native_audio_records))
            if rec.get("audio_streams") == 0:
                raise RuntimeError(
                    f"beat {i}: native-lipsync speaking beat has NO audio "
                    f"stream — r2v returns the re-rendered reference speech "
                    f"baked in, so a silent clip means the generation "
                    f"dropped the audio; concatenating it would ship a mute "
                    f"mouth. Re-roll the clip (manifest['native_audio'] has "
                    f"the source path) or rerun with "
                    f"lipsync_engine='syncso'."
                )
            trim_keep_audio(clip, trim_end, dst, resolution=clip_resolution,
                            extra_vf=accent)
        elif native_lipsync and native_ambient_audio and _has_audio_stream(clip):
            # Non-speaking beats whose clips carry their own AMBIENCE (the
            # generator was asked for room tone / the handling noise of the
            # action): keep that track — replacing it with silence opens an
            # audible dropout between two beats that do have room tone, and
            # throws away sound we paid to render. Probed rather than assumed:
            # a clip that came back mute still needs the synthetic track below.
            trim_keep_audio(clip, window_len, dst, resolution=clip_resolution,
                            extra_vf=accent)
        elif native_lipsync:
            # Native-mode silent beats (reaction holds, b-roll with no
            # dialogue): trim silently to the window, then mux in a synthetic
            # silent aac track so the concat demuxer can stream-copy across
            # the full timeline without codec mismatch. NO ElevenLabs VO
            # slice is attached — native mode never mixes in the oracle VO.
            silent = work / f"beat-{i}-silent.mp4"
            trim_clip(clip, window_len, silent, resolution=clip_resolution,
                      extra_vf=accent)
            attach_silence(silent, dst, window_len, resolution=clip_resolution)
        elif isolated_audio:
            # isolated-audio non-on_camera beats: background_vo gets its
            # per-beat isolated audio attached; silent/reaction beats get a
            # synthetic silent track. In both cases we trim to the clip's
            # audio-derived duration (or window_len for silent beats, which
            # have no per-beat audio).
            _beat_audio_paths = beat_audio_paths or []
            _beat_audio_durs = beat_audio_durs or []
            b_audio = _beat_audio_paths[i] if i < len(_beat_audio_paths) else None
            b_dur = _beat_audio_durs[i] if i < len(_beat_audio_durs) else None
            # Clip duration: audio-derived for speaking beats, window for silent.
            # Cap at the 15s API max — the source clip was ordered at
            # min(15, ceil(audio)); trimming past it would freeze the tail.
            clip_trim_len = (min(15.0, float(b_dur)) if b_dur is not None
                             else window_len)
            silent = work / f"beat-{i}-silent.mp4"
            trim_clip(clip, clip_trim_len, silent, resolution=clip_resolution,
                      extra_vf=accent)
            if voice_mode == "background_vo" and b_audio is not None:
                # Background VO: the per-beat isolated audio becomes the clip's
                # own track (same AAC uniform codec as native mode so the concat
                # demuxer can stream-copy). Pad the audio first so it exactly
                # fills the clip (HeyGen constraint: ≤15% duration mismatch).
                padded = work / f"beat-{i}-audio-padded.wav"
                pad_audio_to(b_audio, clip_trim_len, padded)
                attach_audio(silent, padded, dst, clip_trim_len)
            else:
                # Silent beat (reaction, broll with no dialogue): synthetic
                # silent AAC track for codec uniformity across all clips.
                attach_silence(silent, dst, clip_trim_len,
                               resolution=clip_resolution)
        else:
            trim_clip(clip, window_len, dst, resolution=clip_resolution,
                      extra_vf=accent)
        final_clips[i] = dst

    # 2. Lipsync talking-head beats. Trim-path beats are already final.
    # Sync.so enforces a plan-level concurrency cap (HTTP 429 when exceeded),
    # so the fan-out is bounded by a semaphore (default 1 — the free/basic
    # tier). A beat already present in prior_lipsync_records (a resumed run)
    # reuses its paid Sync.so output instead of re-calling.
    sem = asyncio.Semaphore(max(1, lipsync_concurrency))
    done_records = {r["index"]: r for r in (prior_lipsync_records or [])}

    async def _lipsync_beat(i: int, start: float, end: float,
                            clip: str, src_dur: float | None) -> tuple[int, Path]:
        async with sem:
            # The VO slice covers the FULL clip's span (capped at the VO's end),
            # not just the window — Sync.so syncs every frame it is given, and
            # the window trim afterwards keeps only frames whose mouth matches
            # the VO at exactly that position in the final timeline.
            span = alignment.duration - start
            if src_dur is not None:
                span = min(src_dur, span)
            vo_slice = work / f"beat-{i}-vo.wav"
            slice_audio(vo_audio_path, start, start + span, vo_slice)
            # Seedance i2v clips are audio-LESS. Sync.so accepts a silent video
            # + separate audio, but HeyGen's /v3/lipsyncs validates the input
            # video has an audio stream ("Your video's audio is missing or
            # corrupted") — its model is dub/translate (video-with-audio +
            # replacement-audio). For such engines, mux the VO slice onto the
            # clip first so it carries a valid audio track (full clip length, so
            # the post-sync window trim still holds).
            video_src: str | Path = clip
            if getattr(lipsync_client, "video_needs_audio", False):
                muxed = work / f"beat-{i}-muxed.mp4"
                attach_audio(clip, vo_slice, muxed, src_dur or span)
                video_src = muxed
            video_url = uploader.upload(str(video_src))
            audio_url = uploader.upload(str(vo_slice))
            res = await lipsync_client.lipsync(
                video_url=video_url, audio_url=audio_url,
                output_file_name=f"beat-{i}",
                billed_seconds=src_dur or span,
            )
            synced = work / f"beat-{i}-synced.mp4"
            _download(res.output_url, synced)
            final = work / f"beat-{i}-final.mp4"
            strip_audio(synced, final, duration=end - start,
                        resolution=clip_resolution,
                        extra_vf=corrections[i] or None)
            rec = {
                "index": i, "job_id": res.job_id, "syncso_url": res.output_url,
                "synced_local_path": str(synced),
                "source_local_path": str(clip),
                "vo_slice_local_path": str(vo_slice),
                "input_video_url": video_url, "input_audio_url": audio_url,
            }
            # Persist the instant this (paid) call lands. Safe to mutate shared
            # state here: asyncio is single-threaded and this runs after the
            # await with no preemption point between append and update.
            lipsync_records.append(rec)
            if manifest is not None:
                manifest.update(lipsync=list(lipsync_records))
            if on_asset is not None:
                on_asset("lipsync_clip", rec)
            return i, final

    async def _isolated_lipsync_beat(
        i: int, clip: str, audio_path: str, clip_dur: float,
    ) -> tuple[int, Path]:
        """Per-beat lipsync against the beat's ISOLATED audio
        (heygen_replicate and syncso per_clip engines).

        Pads the beat's isolated audio to EXACTLY the clip duration (the
        heygen/lipsync-precision model rejects inputs whose audio and video
        durations differ by >15%; for sync.so it makes the mapping
        deterministic — sync_mode="silence" is belt-and-braces), submits the
        clip + padded audio to the injected lipsync client, downloads the
        synced output, and trims it to the clip duration while keeping the
        baked audio. No global VO overlay — the synced clip IS the final
        audio for this beat.
        """
        async with sem:
            padded = work / f"beat-{i}-audio-padded.wav"
            pad_audio_to(audio_path, clip_dur, padded)
            # HeyGen models are dub models: they validate that the INPUT video
            # carries an audio stream ("Your video's audio is missing or
            # corrupted") — but Seedance i2v clips are silent. For such clients
            # (video_needs_audio=True) mux the padded beat audio onto the clip
            # first; the same audio is also the lipsync target, so the mouth is
            # re-synced to it and the output carries exactly this beat's audio.
            # Sync.so takes silent video + separate audio directly — no mux.
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
                billed_seconds=clip_dur,
            )
            synced = work / f"beat-{i}-synced.mp4"
            _download(res.output_url, synced)
            # trim_keep_audio: keep the synced audio (it IS the final audio
            # for this beat) and normalise to uniform codec for the concat.
            # Trim to the real speech end + a natural tail (NOT the full ceil'd
            # clip_dur) so a short line doesn't drag ~1s of silent, still-
            # animating mouth into the cut.
            final = work / f"beat-{i}-final.mp4"
            trim_len, tail_fade = _isolated_trim(audio_path, clip_dur)
            trim_keep_audio(synced, trim_len, final, resolution=clip_resolution,
                            tail_fade_s=tail_fade)
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

    tasks = []
    if not native_lipsync and not whole_video_lipsync and not isolated_audio:
        for i, (beat, (start, end)) in enumerate(zip(beats, windows)):
            if beat.shot_type != "talking_head" or _is_reaction(beat):
                continue
            prior = done_records.get(i)
            synced_prior = (Path(prior["synced_local_path"])
                            if prior and prior.get("synced_local_path") else None)
            if synced_prior is not None and synced_prior.exists():
                # Resume: reuse the already-paid Sync.so output, re-deriving the
                # window-trimmed final clip locally (no Sync.so call).
                final = work / f"beat-{i}-final.mp4"
                strip_audio(synced_prior, final, duration=end - start,
                            resolution=clip_resolution,
                            extra_vf=corrections[i] or None)
                final_clips[i] = final
                lipsync_records.append(prior)
                if manifest is not None:
                    manifest.update(lipsync=list(lipsync_records))
                continue
            tasks.append(
                _lipsync_beat(i, start, end, clip_paths[i], src_durs[i])
            )
    elif isolated_audio:
        # isolated-audio engines: fan-out per-beat lipsync for on_camera beats
        # only. background_vo and silent beats were already fully assembled in
        # section 1.
        from app.pipeline import resolve_voice_mode  # noqa: PLC0415
        _beat_audio_paths = beat_audio_paths or []
        _beat_audio_durs = beat_audio_durs or []
        for i, beat in enumerate(beats):
            if resolve_voice_mode(beat) != "on_camera" or _is_reaction(beat):
                continue
            b_audio = _beat_audio_paths[i] if i < len(_beat_audio_paths) else None
            b_dur = _beat_audio_durs[i] if i < len(_beat_audio_durs) else None
            if b_audio is None or b_dur is None:
                # No isolated audio (shouldn't happen for on_camera beats, but
                # guard anyway): fall back to a silence-padded clip.
                dst = final_clips[i]
                if dst is None:
                    silent = work / f"beat-{i}-silent.mp4"
                    trim_clip(clip_paths[i], windows[i][1] - windows[i][0],
                              silent, resolution=clip_resolution)
                    dst = work / f"beat-{i}-trim.mp4"
                    attach_silence(silent, dst, windows[i][1] - windows[i][0],
                                   resolution=clip_resolution)
                    final_clips[i] = dst
                continue
            # Match the generated clip's duration EXACTLY: max(4, min(15, ceil)).
            # The source clip was ordered at this length (Seedance floor 4 /
            # API max 15); trimming past it would freeze/black-pad the tail.
            clip_dur = max(4.0, min(15.0, float(math.ceil(b_dur))))
            # Resume: a prior run may have already produced (and paid for) this
            # beat's synced clip. Reuse it — re-derive the final locally — rather
            # than re-calling the (billed, rate-limited) Replicate model.
            synced_prior = work / f"beat-{i}-synced.mp4"
            if synced_prior.exists():
                final = work / f"beat-{i}-final.mp4"
                prior_len, prior_fade = _isolated_trim(b_audio, clip_dur)
                trim_keep_audio(synced_prior, prior_len, final,
                                resolution=clip_resolution,
                                tail_fade_s=prior_fade)
                final_clips[i] = final
                rec = {"index": i, "job_id": f"resumed-beat-{i}",
                       "output_url": None,
                       "synced_local_path": str(synced_prior),
                       "source_local_path": clip_paths[i],
                       "beat_audio_path": b_audio, "resumed": True}
                lipsync_records.append(rec)
                if manifest is not None:
                    manifest.update(lipsync=list(lipsync_records))
                continue
            tasks.append(_isolated_lipsync_beat(i, clip_paths[i], b_audio, clip_dur))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    for r in results:
        if not isinstance(r, BaseException):
            idx, final = r
            final_clips[idx] = final
    if errors:
        raise RuntimeError(
            f"{len(errors)} lipsync beat(s) failed: {errors[0]!r}"
        ) from errors[0]

    n_lipsynced = len(lipsync_records)
    ordered = [p for p in final_clips if p is not None]

    # 3. Concat. Sync.so mode: silent clips → overlay the full continuous VO.
    # Native mode: every clip already carries its own uniform aac track, so
    # the concat IS the final mix — overlaying the continuous VO would
    # re-introduce the ±0.3s offset the per-clip audio exists to absorb.
    concat_out = work / "concat.mp4"
    stitch(ordered, concat_out)
    # A/V coverage check: every clip is cut to a frame-grid window and Σ
    # windows == VO duration by design, so the concat must match the VO.
    # >0.05s (more than a frame) means a window/trim invariant slipped;
    # >0.25s is a visibly desynced ad — fail loudly even though every clip is
    # already billed, because silently shipping it is worse. In native mode
    # the check is meaningless: per-clip audio makes global drift structurally
    # impossible, and speech-end trims intentionally exceed the windows by up
    # to ~0.4s/beat — informational log only.
    try:
        video_dur: float | None = _probe_duration(concat_out)
    except Exception:
        video_dur = None
    if video_dur is not None:
        drift = abs(video_dur - alignment.duration)
        if native_lipsync or isolated_audio:
            if drift > 0.05:
                engine_label = "native" if native_lipsync else "isolated-audio"
                print(
                    f"[assemble] {engine_label} mode: concat is {video_dur:.2f}s vs "
                    f"{alignment.duration:.2f}s VO — expected (per-beat audio "
                    f"durations differ from VO windows; per-clip audio cannot drift).",
                    file=sys.stderr, flush=True,
                )
        elif drift > 0.25:
            raise RuntimeError(
                f"A/V drift: concatenated video is {video_dur:.2f}s but VO is "
                f"{alignment.duration:.2f}s ({drift:.2f}s apart) — a beat "
                f"window/trim invariant broke; refusing to ship a desynced ad."
            )
        elif drift > 0.05:
            print(
                f"[assemble] A/V drift: concatenated video is {video_dur:.2f}s "
                f"but VO is {alignment.duration:.2f}s — check beat windows.",
                file=sys.stderr, flush=True,
            )
    # 3.5 Whole-video Sync.so pass ("whole" granularity, syncso only): a SINGLE
    # lipsync call over the entire silent concat + the FULL VO, instead of N
    # per-clip calls. The synced output already carries the VO (sync_mode=silence
    # keeps the full video length), so there is no per-clip overlay downstream —
    # ``audio_baked_in`` routes it through the same copy/burn finish as native
    # mode. Resume-safe: the one job is persisted to manifest["lipsync"] with
    # ``whole_video: True`` and reused on a rerun instead of re-billing.
    audio_baked_in = native_lipsync or isolated_audio
    final_video = concat_out
    if whole_video_lipsync and not native_lipsync:
        prior_whole = next(
            (r for r in (prior_lipsync_records or []) if r.get("whole_video")),
            None,
        )
        prior_path = (
            Path(prior_whole["synced_local_path"])
            if prior_whole and prior_whole.get("synced_local_path") else None
        )
        if prior_path is not None and prior_path.exists():
            rec = prior_whole                       # reuse the already-paid pass
            final_video = prior_path
        else:
            video_url = uploader.upload(str(concat_out))
            audio_url = uploader.upload(str(vo_audio_path))
            res = await lipsync_client.lipsync(
                video_url=video_url, audio_url=audio_url,
                output_file_name="whole",
                billed_seconds=_probe_duration(concat_out),
            )
            synced_whole = work / "whole-synced.mp4"
            _download(res.output_url, synced_whole)
            final_video = synced_whole
            rec = {
                "index": -1, "whole_video": True, "job_id": res.job_id,
                "syncso_url": res.output_url,
                "synced_local_path": str(synced_whole),
                "source_local_path": str(concat_out),
                "input_video_url": video_url, "input_audio_url": audio_url,
            }
        lipsync_records.append(rec)
        n_lipsynced = 1
        if manifest is not None:
            manifest.update(lipsync=list(lipsync_records))
        if on_asset is not None:
            on_asset("lipsync_clip", rec)
        audio_baked_in = True

    # 4. Finishing. Audio and video are finished in exactly two steps —
    # master_audio() (room tone + fade + two-pass loudnorm) and finish() (the
    # single CRF18 re-encode: grade → grain → captions → fade + BT.709 +
    # 192k AAC mux). Baked-audio modes master the concat's own mix; overlay
    # modes master the ORIGINAL VO wav directly (never a lossy intermediate)
    # and finish() muxes it — overlay_audio's separate pass is gone.
    if audio_baked_in:
        audio_src: str | Path = final_video
        final_dur = video_dur if video_dur is not None else alignment.duration
    else:
        audio_src = vo_audio_path
        final_dur = alignment.duration

    caption_words = None
    if burn_captions:
        caption_words = await _caption_words(
            alignment=alignment, beat_alignments=beat_alignments,
            ordered=ordered, native_lipsync=native_lipsync,
            isolated_audio=isolated_audio, beats=beats,
            final_video=final_video, force_align=force_align,
        )
    ass_path: Path | None = None
    if caption_words is not None:
        w, h = _dims(clip_resolution)
        ass_path = work / "captions.ass"
        ass_path.write_text(build_ass(
            caption_words, width=w, height=h,
            words_per_line=caption_words_per_line,
            emphasis_terms=emphasis_terms, total_duration=final_dur,
        ))

    mastered = work / "master.wav"
    master_audio(audio_src, mastered, duration=final_dur,
                 room_tone=room_tone, i=loudnorm_i)
    finish(final_video, mastered, out_path, duration=final_dur,
           ass_path=ass_path, grade=finishing_grade, look=finishing_look)

    return AssembledVideo(
        path=str(out_path),
        duration_s=(video_dur if (native_lipsync or isolated_audio)
                    and video_dur is not None
                    else alignment.duration),
        n_beats=len(beats),
        n_lipsynced=n_lipsynced,
    )


def _download(url: str, dest: Path) -> None:
    """Download with a browser UA (CDNs block Python-urllib)."""
    download(url, dest)
