import shutil
import subprocess

import pytest

from app.grading import (
    LOOKS,
    finishing_vf,
    match_corrections,
    measure_clip_stats,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_finishing_vf_appends_grain_to_look():
    vf = finishing_vf("clean_bright")
    assert vf.startswith(LOOKS["clean_bright"])
    assert vf.endswith("noise=alls=5:allf=t+u")


def test_finishing_vf_none_is_grain_only():
    assert finishing_vf("none") == "noise=alls=5:allf=t+u"
    # Unknown look degrades to grain-only rather than a broken filtergraph.
    assert finishing_vf("bogus") == "noise=alls=5:allf=t+u"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")
@pytest.mark.parametrize("look", [k for k in LOOKS if k != "none"])
def test_every_look_is_a_valid_filtergraph(look, tmp_path):
    """Each look chain must actually encode — a typo'd filter option would
    otherwise only surface on a paid render's finishing pass."""
    out = tmp_path / f"{look}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=180x320:d=0.5",
         "-vf", finishing_vf(look), "-pix_fmt", "yuv420p", str(out)],
        check=True, capture_output=True,
    )
    assert out.stat().st_size > 0


def test_match_corrections_pull_toward_median_with_caps():
    stats = [
        {"luma": 0.50, "rg": 0.00, "bg": 0.00},   # the median-ish clip
        {"luma": 0.30, "rg": 0.00, "bg": 0.00},   # way dark → capped luma
        {"luma": 0.50, "rg": 0.20, "bg": -0.20},  # strong warm cast → capped
        {"luma": 0.505, "rg": 0.002, "bg": 0.0},  # negligible → no correction
    ]
    corr = match_corrections(stats)
    assert corr[0] == ""
    # Dark clip: brightness pull is CAPPED at +0.04, not the raw +0.20.
    assert "eq=brightness=0.040" in corr[1]
    # Cast clip: colorbalance capped at ±0.10 (looser cast cap matches white
    # balance across cuts).
    assert "colorbalance=rm=-0.100:bm=0.100" in corr[2]
    assert corr[3] == ""


def test_match_corrections_skip_unmeasurable_and_small_sets():
    assert match_corrections([None, {"luma": 0.5, "rg": 0, "bg": 0}]) == ["", ""]
    corr = match_corrections([
        {"luma": 0.50, "rg": 0.0, "bg": 0.0},
        None,
        {"luma": 0.40, "rg": 0.0, "bg": 0.0},
    ])
    assert corr[1] == ""             # unmeasurable clip untouched
    assert "brightness" in corr[2]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")
def test_measure_clip_stats_reads_levels(tmp_path):
    dark = tmp_path / "dark.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x202020:s=180x320:d=3",
         "-pix_fmt", "yuv420p", str(dark)], check=True, capture_output=True,
    )
    bright = tmp_path / "bright.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0xC0C0C0:s=180x320:d=3",
         "-pix_fmt", "yuv420p", str(bright)], check=True, capture_output=True,
    )
    s_dark = measure_clip_stats(dark)
    s_bright = measure_clip_stats(bright)
    assert s_dark is not None and s_bright is not None
    assert s_dark["luma"] < 0.2 < 0.6 < s_bright["luma"]
    assert measure_clip_stats(tmp_path / "missing.mp4") is None
