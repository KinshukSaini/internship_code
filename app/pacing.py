"""Audio-domain dead-air trimming for the VO track.

Runs BEFORE beat_windows: the VO and its character alignment are the master
clock for the whole pipeline, so silences must be cut from BOTH together, up
front — every downstream consumer (beat_windows, split_long_windows, lipsync
slices, captions) then plans against the already-paced timeline and the
Σ windows == VO duration invariant holds with no special cases.

Audio-domain ONLY: Sync.so generates mouth motion against the exact VO slice
it receives, so any post-lipsync retiming of the assembled video
(setpts/atempo) would desync every mouth. Removing dead air from the VO
before any clip or lipsync exists is the only safe place to change pacing.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.voice import Alignment

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


def _detect_silences(
    vo_path: str | Path, threshold_db: float, min_silence_s: float,
) -> list[tuple[float, float]]:
    """Run silencedetect; return ordered (start, end) silence intervals.

    silencedetect logs to stderr only — the null muxer discards the media.
    A dangling silence_start with no matching end (trailing silence at EOF
    on older ffmpeg builds) is ignored: audio past the last spoken char is
    beyond every beat window anyway.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(vo_path),
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_s}",
         "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for line in proc.stderr.splitlines():
        m = _SILENCE_START_RE.search(line)
        if m:
            pending = max(0.0, float(m.group(1)))
            continue
        m = _SILENCE_END_RE.search(line)
        if m and pending is not None:
            silences.append((pending, float(m.group(1))))
            pending = None
    return silences


def _remap(t: float, removals: list[tuple[float, float]]) -> float:
    """Shift t earlier by the total length of removals fully before it.

    Chars never fall inside a removed span (it was silence), but clamp
    defensively to the span's left edge if one ever does.
    """
    removed = 0.0
    for r0, r1 in removals:
        if t >= r1:
            removed += r1 - r0
        elif t > r0:
            return r0 - removed
        else:
            break
    return t - removed


def trim_silences(
    vo_path: str | Path, alignment: Alignment, *, work_dir: str | Path,
    threshold_db: float = -30.0, min_silence_s: float = 0.30,
    pad_s: float = 0.10,
) -> tuple[str, Alignment]:
    """Cut dead air out of the VO and rebuild the alignment to match.

    Every detected silence LONGER than ``min_silence_s`` keeps ``pad_s`` at
    each edge and loses the middle (a 0.9s gap becomes 0.2s). All kept
    segments are cut in ONE ffmpeg pass (atrim+concat) to
    <work_dir>/vo-paced.wav — pcm wav, so the already-lossy mp3 VO is not
    re-encoded through mp3 a second time; downstream ffmpeg consumers
    (slice_audio, master_audio) accept wav. If no silence qualifies, the
    inputs are returned untouched with no cut pass.
    """
    silences = _detect_silences(vo_path, threshold_db, min_silence_s)
    removals: list[tuple[float, float]] = []
    for s0, s1 in silences:
        r0, r1 = s0 + pad_s, s1 - pad_s
        # 1ms epsilon so float noise (0.8-0.5 > 0.3) can't qualify a silence
        # that is exactly min_silence_s long.
        if s1 - s0 > min_silence_s + 1e-3 and r1 > r0:
            removals.append((r0, r1))
    if not removals:
        return str(vo_path), alignment

    # Kept segments = complement of the removals. The last one is open-ended
    # (atrim without :end) so any audio past the final removal survives.
    kept: list[tuple[float, float | None]] = []
    prev = 0.0
    for r0, r1 in removals:
        if r0 > prev:
            kept.append((prev, r0))
        prev = r1
    kept.append((prev, None))

    # asetpts=PTS-STARTPTS resets each segment's clock — atrim keeps source
    # timestamps, and concat needs every input to start at 0 to be gapless.
    parts: list[str] = []
    for i, (s, e) in enumerate(kept):
        spec = f"atrim=start={s:.3f}"
        if e is not None:
            spec += f":end={e:.3f}"
        parts.append(f"[0:a]{spec},asetpts=PTS-STARTPTS[k{i}]")
    labels = "".join(f"[k{i}]" for i in range(len(kept)))
    parts.append(f"{labels}concat=n={len(kept)}:v=0:a=1[out]")

    out = Path(work_dir) / "vo-paced.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(vo_path),
         "-filter_complex", ";".join(parts), "-map", "[out]",
         "-c:a", "pcm_s16le", "-ar", "44100", str(out)],
        check=True, capture_output=True,
    )

    return str(out), Alignment(
        characters=list(alignment.characters),
        starts=[_remap(t, removals) for t in alignment.starts],
        ends=[_remap(t, removals) for t in alignment.ends],
    )
