import subprocess
from pathlib import Path

import pytest

import app.pacing as P
from app.pacing import trim_silences
from app.voice import Alignment


def _align(chars: str, starts: list[float], ends: list[float]) -> Alignment:
    return Alignment(characters=list(chars), starts=starts, ends=ends)


class _FakeRun:
    """Stands in for subprocess.run: records every command and returns the
    canned silencedetect stderr (the cut pass ignores it)."""

    def __init__(self, stderr: str = ""):
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=self.stderr)


# A 0.9s gap between 'A' (0.0-0.5) and 'B' (1.4-1.9).
_GAP_09_LOG = (
    "[silencedetect @ 0x600] silence_start: 0.5\n"
    "[silencedetect @ 0x600] silence_end: 1.4 | silence_duration: 0.9\n"
)


def test_trim_silences_cuts_in_one_ffmpeg_pass(tmp_path, monkeypatch):
    """A 0.9s gap keeps 0.1s pad each side and loses the 0.7s middle, cut in
    a single atrim+concat invocation to <work_dir>/vo-paced.wav."""
    fake = _FakeRun(stderr=_GAP_09_LOG)
    monkeypatch.setattr(P.subprocess, "run", fake)
    al = _align("AB", [0.0, 1.4], [0.5, 1.9])

    path, new_al = trim_silences("vo.mp3", al, work_dir=tmp_path)

    assert path == str(tmp_path / "vo-paced.wav")
    assert len(fake.calls) == 2                      # detect + ONE cut pass
    detect, cut = fake.calls
    assert "silencedetect=noise=-30.0dB:d=0.3" in detect
    fc = cut[cut.index("-filter_complex") + 1]
    assert "atrim=start=0.000:end=0.600" in fc       # keep head + pad
    assert "atrim=start=1.300," in fc                # open-ended tail segment
    assert "concat=n=2:v=0:a=1" in fc
    assert "pcm_s16le" in cut                        # pcm wav, no mp3 re-encode
    # Removal is (0.6, 1.3) → chars after it shift earlier by 0.7s.
    assert new_al.starts == pytest.approx([0.0, 0.7])
    assert new_al.ends == pytest.approx([0.5, 1.2])
    assert new_al.characters == al.characters
    assert new_al.duration == pytest.approx(al.duration - 0.7)


def test_offset_shifts_chars_after_trimmed_gap(tmp_path, monkeypatch):
    """A 0.7s silence with pad 0.1 at each edge removes 0.5s — every char
    timestamp after the gap shifts earlier by exactly 0.5s."""
    log = ("[silencedetect @ 0x0] silence_start: 0.5\n"
           "[silencedetect @ 0x0] silence_end: 1.2 | silence_duration: 0.7\n")
    fake = _FakeRun(stderr=log)
    monkeypatch.setattr(P.subprocess, "run", fake)
    al = _align("AB", [0.0, 1.2], [0.5, 1.7])

    _, new_al = trim_silences("vo.mp3", al, work_dir=tmp_path)

    assert new_al.starts[0] == 0.0 and new_al.ends[0] == 0.5   # before gap
    assert new_al.starts[1] == pytest.approx(0.7)              # 1.2 - 0.5
    assert new_al.ends[1] == pytest.approx(1.2)                # 1.7 - 0.5
    assert new_al.duration == pytest.approx(al.duration - 0.5)


def test_piecewise_cumulative_offsets_over_two_gaps(tmp_path, monkeypatch):
    """Chars after the second gap shift by the SUM of both removals; chars
    between the gaps shift only by the first."""
    log = ("[silencedetect @ 0x0] silence_start: 0.5\n"
           "[silencedetect @ 0x0] silence_end: 1.4 | silence_duration: 0.9\n"
           "[silencedetect @ 0x0] silence_start: 2.0\n"
           "[silencedetect @ 0x0] silence_end: 2.6 | silence_duration: 0.6\n")
    fake = _FakeRun(stderr=log)
    monkeypatch.setattr(P.subprocess, "run", fake)
    # 'A' 0.0-0.5, 'B' 1.4-2.0, 'C' 2.6-3.0; removals 0.7s then 0.4s.
    al = _align("ABC", [0.0, 1.4, 2.6], [0.5, 2.0, 3.0])

    _, new_al = trim_silences("vo.mp3", al, work_dir=tmp_path)

    assert new_al.starts == pytest.approx([0.0, 0.7, 1.5])
    assert new_al.ends == pytest.approx([0.5, 1.3, 1.9])
    assert new_al.duration == pytest.approx(al.duration - 1.1)
    fc = fake.calls[1][fake.calls[1].index("-filter_complex") + 1]
    assert "concat=n=3:v=0:a=1" in fc                # 3 kept segments, one pass


def test_noop_when_no_silence_qualifies(tmp_path, monkeypatch):
    """A silence exactly min_silence_s long is NOT trimmed (must be longer),
    and a dangling silence_start with no end is ignored — so the original
    path and alignment come back with no cut pass."""
    log = ("[silencedetect @ 0x0] silence_start: 0.5\n"
           "[silencedetect @ 0x0] silence_end: 0.8 | silence_duration: 0.3\n"
           "[silencedetect @ 0x0] silence_start: 2.0\n")
    fake = _FakeRun(stderr=log)
    monkeypatch.setattr(P.subprocess, "run", fake)
    al = _align("AB", [0.0, 0.8], [0.5, 1.3])

    path, new_al = trim_silences("vo.mp3", al, work_dir=tmp_path)

    assert path == "vo.mp3"
    assert new_al is al
    assert len(fake.calls) == 1                      # detection only
    assert not (tmp_path / "vo-paced.wav").exists()


def test_noop_when_no_silence_detected(tmp_path, monkeypatch):
    fake = _FakeRun(stderr="")
    monkeypatch.setattr(P.subprocess, "run", fake)
    al = _align("AB", [0.0, 0.5], [0.5, 1.0])

    path, new_al = trim_silences("vo.mp3", al, work_dir=tmp_path)

    assert path == "vo.mp3"
    assert new_al is al
    assert len(fake.calls) == 1


def test_clamps_timestamps_inside_removed_span(tmp_path, monkeypatch):
    """Chars are never silent so they shouldn't sit inside a removal, but a
    timestamp that does is clamped to the removal's left edge instead of
    going negative or non-monotonic."""
    fake = _FakeRun(stderr=_GAP_09_LOG)
    monkeypatch.setattr(P.subprocess, "run", fake)
    # 'B' start 1.0 sits inside the removed span (0.6, 1.3).
    al = _align("AB", [0.0, 1.0], [0.5, 1.9])

    _, new_al = trim_silences("vo.mp3", al, work_dir=tmp_path)

    assert new_al.starts[1] == pytest.approx(0.6)    # clamped to span start
    assert new_al.ends[1] == pytest.approx(1.2)      # 1.9 - 0.7
    assert new_al.starts[1] <= new_al.ends[1]
