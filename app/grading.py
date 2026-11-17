"""Colour pipeline: selectable finishing looks + per-clip shot matching.

Recipes come from the July-2026 post-production research (see
docs/production-polish-plan-2026-07.md): every look pivots its tone curve at
mid-grey so skin brightness doesn't shift, saturation moves ride `vibrance`
or hue-selective `huesaturation` (skin-protecting) rather than raw
saturation multipliers, and everything stays inside the "over-grading tells"
guardrails (no crushed blacks, no matte lift past 0.08, no sat >1.15).

Shot matching: AI clips from the same generator drift in exposure and cast
between generations. Each source clip's mean luma + colour cast is measured
from sampled frames, the MEDIAN across clips becomes the reference (median,
not clip 1 — one rogue clip must not drag the whole ad), and each clip gets
a tightly CAPPED eq/colorbalance correction baked into its existing trim
re-encode. Caps matter more than accuracy: a slightly-off clip beats a
broken one, and coherence — not identity — is the goal (dead-flat
uniformity itself reads synthetic).
"""
from __future__ import annotations

import statistics
import subprocess
from pathlib import Path

# ---- Finishing looks -------------------------------------------------------

LOOKS: dict[str, str] = {
    "none": "",
    # Safest UGC default: airy lifted curve, whisper of cool in shadows /
    # warm in highlights, hue-selective skin lift.
    "clean_bright": (
        "curves=master='0/0.05 0.25/0.28 0.75/0.80 1/0.98',"
        "colorbalance=bs=0.03:rh=0.02,"
        "huesaturation=saturation=0.10:colors=r+y,"
        "eq=brightness=0.03:saturation=1.05"
    ),
    # Golden-hour: warm WB shift (>6500 = warmer), amber highlights, gentle
    # lifted curve. colortemperature mix keeps it a look, not a cast.
    "warm_golden": (
        "colortemperature=temperature=7400:mix=0.35:pl=1,"
        "colorbalance=rs=0.03:bs=-0.03:rh=0.04:bh=-0.04,"
        "curves=master='0/0.04 0.5/0.5 1/0.97',"
        "vibrance=intensity=0.12"
    ),
    # Punchy split-tone, deliberately restrained (the research values blew
    # out skin on real footage — verified on frames). Not the default.
    "teal_orange": (
        "colorbalance=rs=-0.03:gs=0.01:bs=0.04:rh=0.02:gh=0.01:bh=-0.02:pl=1,"
        "curves=master='0/0 0.25/0.22 0.75/0.78 1/0.97',"
        "vibrance=intensity=0.15"
    ),
}

# Subtle temporal sensor grain: breaks AI-clean plasticity and dithers the
# banding that any contrast move creates on flat AI gradients. alls=5 is
# below the visible-shimmer threshold (user feedback 2026-07-09).
_GRAIN = "noise=alls=5:allf=t+u"


def finishing_vf(look: str) -> str:
    """The grade portion of the single finishing encode: look, then grain."""
    chain = LOOKS.get(look, "")
    return f"{chain},{_GRAIN}" if chain else _GRAIN


# ---- Per-clip shot matching ------------------------------------------------

# Correction caps (research: clamp hard; partial correction beats a broken
# clip). Luma as eq brightness (±), casts as colorbalance midtone offsets.
# Luma stays conservative (brightness over-correction bands/crushes). The CAST
# cap is looser (0.10): independently-generated Seedance clips drift in white
# balance (a warm bed shot vs a cooler standing shot read as different grades
# across a cut — a real QC flag), and midtone colorbalance at ±0.10 neutralises
# that gap toward the median without visible artefacting.
_LUMA_CAP = 0.04
_CAST_CAP = 0.10
_NEGLIGIBLE = 0.008
_SAMPLE_SIZE = 48   # frames are downscaled to 48x48 before measuring


def measure_clip_stats(path: str | Path,
                       sample_times: tuple[float, ...] = (0.3, 1.0, 2.0),
                       ) -> dict | None:
    """Mean luma + colour cast of a clip from a few downscaled frames.

    Returns {"luma": 0..1, "rg": r-g cast, "bg": b-g cast} or None when the
    clip can't be read (caller treats None as "leave this clip alone").
    """
    px: list[tuple[float, float, float]] = []
    for t in sample_times:
        try:
            raw = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1",
                 "-vf", f"scale={_SAMPLE_SIZE}:{_SAMPLE_SIZE}",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True, check=True,
            ).stdout
        except Exception:
            continue
        for o in range(0, len(raw) - 2, 3):
            px.append((raw[o] / 255, raw[o + 1] / 255, raw[o + 2] / 255))
    if not px:
        return None
    n = len(px)
    r = sum(p[0] for p in px) / n
    g = sum(p[1] for p in px) / n
    b = sum(p[2] for p in px) / n
    return {"luma": 0.2126 * r + 0.7152 * g + 0.0722 * b,
            "rg": r - g, "bg": b - g}


def _clamp(v: float, cap: float) -> float:
    return max(-cap, min(cap, v))


def match_corrections(stats: list[dict | None]) -> list[str]:
    """Per-clip ffmpeg correction snippets matching every clip toward the
    MEDIAN clip's exposure and cast. Empty string = no correction needed
    (or clip unmeasurable)."""
    usable = [s for s in stats if s is not None]
    if len(usable) < 2:
        return [""] * len(stats)
    ref_luma = statistics.median(s["luma"] for s in usable)
    ref_rg = statistics.median(s["rg"] for s in usable)
    ref_bg = statistics.median(s["bg"] for s in usable)
    out: list[str] = []
    for s in stats:
        if s is None:
            out.append("")
            continue
        d_luma = _clamp(ref_luma - s["luma"], _LUMA_CAP)
        d_rm = _clamp((ref_rg - s["rg"]) * 0.8, _CAST_CAP)
        d_bm = _clamp((ref_bg - s["bg"]) * 0.8, _CAST_CAP)
        parts: list[str] = []
        if abs(d_luma) >= _NEGLIGIBLE:
            parts.append(f"eq=brightness={d_luma:.3f}")
        if abs(d_rm) >= _NEGLIGIBLE or abs(d_bm) >= _NEGLIGIBLE:
            parts.append(f"colorbalance=rm={d_rm:.3f}:bm={d_bm:.3f}")
        out.append(",".join(parts))
    return out
