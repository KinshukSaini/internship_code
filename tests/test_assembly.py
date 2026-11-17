import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.assembly import (
    _NATIVE_TAIL_FADE_S, apply_holds, assemble, beat_windows, finish,
    head_accent_vf, master_audio, merge_short_windows, pad_audio_to,
    punch_in_vf, split_long_windows, strip_audio, trim_clip,
)
from app.voice import Alignment

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None


@dataclass
class _Beat:
    index: int
    dialogue_text: str
    shot_type: str
    framing: str | None = None
    broll_shot: str | None = None
    visual_note: str = ""
    beat_kind: str = "speech"
    hold_s: float | None = None
    shows_creator: bool = True
    voice_mode: str | None = None


def _linear_alignment(text: str, step: float = 0.1) -> Alignment:
    chars = list(text)
    starts = [i * step for i in range(len(chars))]
    ends = [(i + 1) * step for i in range(len(chars))]
    return Alignment(characters=chars, starts=starts, ends=ends)


def test_beat_windows_maps_contiguous_slices():
    al = _linear_alignment("Hello world")  # 11 chars, 0.0..1.1
    beats = [
        _Beat(0, "Hello", "talking_head"),
        _Beat(1, " world", "broll"),
    ]
    wins = beat_windows(beats, al)
    assert wins[0] == pytest.approx((0.0, 0.5))   # 0.5 == 12 frames: on-grid
    assert wins[1] == pytest.approx((0.5, 1.1))
    # Windows are contiguous and cover the whole VO.
    assert wins[0][1] == pytest.approx(wins[1][0])


def test_beat_windows_strips_audio_tags():
    """An [excited] tag in dialogue_text must not drift the window: the tag is
    absent from the alignment (API consumes it), so we match the stripped text
    against the alignment's own tag-free character string."""
    # Spoken output (what the API returns) is tag-free: "Hello world"
    al = _linear_alignment("Hello world")   # 11 chars, 0.0..1.1
    beats = [
        # dialogue carries the tag; the spoken slice is just "Hello"
        _Beat(0, "[excited]Hello", "talking_head"),
        _Beat(1, " world", "broll"),
    ]
    wins = beat_windows(beats, al)
    assert wins[0] == pytest.approx((0.0, 0.5))   # "Hello" → chars 0..4
    assert wins[1] == pytest.approx((0.5, 1.1))   # " world" → chars 5..10


def test_beat_windows_no_backward_binding_on_repeat():
    """A repeated slice must bind FORWARD, never jump back to an earlier
    occurrence. With align 'ABAB', beat0='ABA' consumes chars 0-2; beat1='A'
    has no occurrence at/after the cursor, so it must stay at the cursor
    (forward/monotonic) — the old global-find() fallback jumped it back to
    char 0 and drifted the cut. The cursor boundary (raw 0.3) snaps to the
    24fps frame grid: round(0.3 * 24) / 24 == 7/24."""
    al = _linear_alignment("ABAB")   # 4 chars, 0.0..0.4
    beats = [_Beat(0, "ABA", "talking_head"), _Beat(1, "A", "broll")]
    wins = beat_windows(beats, al)
    assert wins[1][0] >= wins[0][0]              # never earlier than beat 0
    assert wins[1][0] >= wins[0][1] - 1e-9       # forward of beat 0's end
    assert wins[0] == pytest.approx((0.0, 7 / 24))
    assert wins[1] == pytest.approx((7 / 24, 0.4))  # bound at the cursor, not 0.0


def test_beat_windows_guards_zero_window():
    al = _linear_alignment("hi")
    beats = [_Beat(0, "", "broll")]   # empty dialogue
    wins = beat_windows(beats, al)
    assert wins[0][1] > wins[0][0]   # widened to a non-zero window


def test_beat_windows_contiguous_over_audio_tag_hole():
    """An audible audio tag ([sighs]) has NO characters in the alignment, so
    its audio used to fall in no window — breaking Σ windows == VO duration
    and desyncing every beat after it (~0.92s in a real run). Windows must be
    a contiguous partition: the tag's audio belongs to the FOLLOWING beat."""
    # 'Nice. ' spoken 0.0-0.6s; the sigh occupies 0.6-1.6s with no chars;
    # 'Girls love it.' spoken 1.6-3.0s.
    chars = list("Nice. ") + list("Girls love it.")
    starts = [i * 0.1 for i in range(6)] + [1.6 + i * 0.1 for i in range(14)]
    ends = [s + 0.1 for s in starts]
    al = Alignment(characters=chars, starts=starts, ends=ends)
    beats = [
        _Beat(0, "Nice. ", "talking_head"),
        _Beat(1, "[sighs] Girls love it.", "talking_head"),
    ]
    wins = beat_windows(beats, al)
    assert wins[0][1] == pytest.approx(wins[1][0])      # no hole
    # Sigh → beat 1's window; the raw 0.6 boundary snaps to the frame grid
    # (round(0.6 * 24) / 24 == 14/24).
    assert wins[1][0] == pytest.approx(14 / 24)
    assert wins[0][0] == 0.0
    assert wins[-1][1] == pytest.approx(al.duration)    # covers the full VO
    assert sum(e - s for s, e in wins) == pytest.approx(al.duration)


def test_beat_windows_snaps_interior_boundaries_to_frame_grid():
    """Every interior boundary lands on the 24fps frame grid (so each clip's
    -t encodes a whole number of frames); the final end stays the VO's true,
    unrounded end and every window keeps ≥ 2 frames."""
    al = _linear_alignment("abcdefg hijkl mnopq", step=0.07)   # off-grid ends
    beats = [
        _Beat(0, "abcdefg ", "talking_head"),
        _Beat(1, "hijkl ", "talking_head"),
        _Beat(2, "mnopq", "broll"),
    ]
    wins = beat_windows(beats, al)
    for _s, e in wins[:-1]:
        assert e * 24 == pytest.approx(round(e * 24), abs=1e-6)
    assert wins[0][0] == 0.0
    assert wins[-1][1] == pytest.approx(al.duration)    # true end, unrounded
    for (_s0, e0), (s1, _e1) in zip(wins, wins[1:]):
        assert s1 == e0                                 # contiguous
    assert all((e - s) >= 2 / 24 - 1e-9 for s, e in wins)


def test_beat_windows_snaps_mid_word_boundary_to_word_gap():
    """The planner partitions full_script but is not forced to cut between
    words, so a beat can end mid-word. The boundary must snap to the nearest
    word gap (a space char's start/end) — otherwise the word's audio splits
    across two beats, and in native mode the two halves come from different
    sources (baked talking-head speech vs the B-roll's VO slice), so the word
    is heard clipped at the cut. With align 'one fantastic two', a beat that
    ends at 'fan' must extend its window to the gap after 'fantastic'."""
    al = _linear_alignment("one fantastic two")   # spaces at idx 3 and 13
    beats = [
        _Beat(0, "one fan", "talking_head"),       # ends INSIDE 'fantastic'
        _Beat(1, "tastic two", "broll"),
    ]
    wins = beat_windows(beats, al)
    # The raw boundary (end of 'n' in 'fan' = 0.7) splits 'fantastic'. The
    # interior boundary must land in an inter-word gap (a space char's start or
    # end), snapped to the frame grid — so neither beat's audio slice cuts a
    # word. Any gap is acceptable; what matters is it is NOT mid-word.
    gap_times = {al.starts[i] for i, c in enumerate(al.characters) if c == " "}
    gap_times |= {al.ends[i] for i, c in enumerate(al.characters) if c == " "}
    grid_gaps = {round(round(t * 24) / 24, 6) for t in gap_times}
    assert round(wins[0][1], 6) in grid_gaps             # boundary in a gap
    assert wins[0][1] == pytest.approx(wins[1][0])       # contiguous
    assert wins[-1][1] == pytest.approx(al.duration)     # covers full VO
    assert sum(e - s for s, e in wins) == pytest.approx(al.duration)


def test_split_long_windows_splits_at_word_boundaries():
    """A 12s runaway window (past the ~5s fast-cut ceiling, 2026-07-31) must
    split into four ~3s sub-windows, contiguous, cut on word boundaries snapped
    to the 24fps frame grid, with sub-beats inheriting the parent's fields and
    carrying sub_index. (Windows ≤5s are held whole — see
    test_split_long_windows_noop_*.)"""
    text = "one two three four five six seven eight nine ten more"
    n = len(text)
    step = 12.0 / n
    al = Alignment(
        characters=list(text),
        starts=[i * step for i in range(n)],
        ends=[(i + 1) * step for i in range(n)],
    )
    beats = [_Beat(0, text, "talking_head", framing="selfie")]
    nb, nw = split_long_windows(beats, [(0.0, al.duration)], al)

    assert len(nb) == 4 and len(nw) == 4                # ceil(12/3) = 4 subs
    assert all((e - s) <= 5.0 for s, e in nw)
    for (s0, e0), (s1, _e1) in zip(nw, nw[1:]):
        assert e0 == pytest.approx(s1)                  # contiguous
    assert nw[0][0] == 0.0 and nw[-1][1] == pytest.approx(al.duration)
    # Each interior cut is the nearest word boundary (a space char's end time)
    # snapped to the 24fps frame grid.
    snapped_space_ends = {
        round(round(al.ends[i] * 24) / 24, 6)
        for i, ch in enumerate(al.characters) if ch == " "
    }
    for _s, e in nw[:-1]:
        assert round(e, 6) in snapped_space_ends
        assert e * 24 == pytest.approx(round(e * 24), abs=1e-6)
    # Sub-beats inherit the parent's shot data, are reindexed, and carry their
    # position within the parent's jump-cut run.
    assert all(b.shot_type == "talking_head" and b.framing == "selfie"
               for b in nb)
    assert [b.index for b in nb] == [0, 1, 2, 3]
    assert [b.sub_index for b in nb] == [0, 1, 2, 3]


def test_split_long_windows_chunks_dialogue_per_subbeat():
    """Regression: a split talking beat must NOT leave the FULL line on every
    sub-beat. heygen_replicate generates a fresh per-beat TTS and seedance_native
    embeds the line in the prompt, so duplicated dialogue would re-utter the whole
    sentence k times (and over-bill). Each sub-beat must get its own contiguous
    word-chunk; the chunks reconstruct the original words in order."""
    text = "one two three four five six seven eight nine ten more"
    n = len(text)
    step = 12.0 / n
    al = Alignment(characters=list(text),
                   starts=[i * step for i in range(n)],
                   ends=[(i + 1) * step for i in range(n)])
    beats = [_Beat(0, text, "talking_head", framing="selfie")]
    nb, _nw = split_long_windows(beats, [(0.0, al.duration)], al)

    assert len(nb) > 1
    chunks = [b.dialogue_text for b in nb]
    assert all(c.strip() for c in chunks)          # every sub-beat has words
    assert all(c != text for c in chunks)          # none carries the full line
    assert len(set(chunks)) == len(chunks)         # distinct chunks
    # Chunks are contiguous word-slices reconstructing the original words.
    assert " ".join(chunks).split() == text.split()


def test_split_long_windows_noop_for_short_windows():
    al = _linear_alignment("Hello world")
    beats = [_Beat(0, "Hello", "talking_head"), _Beat(1, " world", "broll")]
    wins = [(0.0, 0.5), (0.5, 1.1)]
    nb, nw = split_long_windows(beats, wins, al)
    assert nw == wins                       # nothing over 5s → untouched
    assert [b.index for b in nb] == [0, 1]
    assert [b.shot_type for b in nb] == ["talking_head", "broll"]
    assert [b.sub_index for b in nb] == [0, 0]   # pass-through beats are sub 0


def test_merge_short_windows_merges_short_middle_into_previous():
    beats = [
        _Beat(0, "a", "talking_head", framing="A"),
        _Beat(1, "b", "talking_head", framing="B"),
        _Beat(2, "c", "talking_head", framing="C"),
    ]
    wins = [(0.0, 3.0), (3.0, 4.0), (4.0, 7.0)]   # middle is 1.0s < 2.2s
    nb, nw = merge_short_windows(beats, wins)
    assert nw == [(0.0, 4.0), (4.0, 7.0)]         # combined span, no hole
    assert [b.framing for b in nb] == ["A", "C"]  # longer/first beat's fields
    assert [b.index for b in nb] == [0, 1]        # reindexed
    # Pure: the inputs were not mutated.
    assert [b.index for b in beats] == [0, 1, 2]
    assert wins == [(0.0, 3.0), (3.0, 4.0), (4.0, 7.0)]


def test_merge_short_windows_short_first_merges_into_next():
    beats = [
        _Beat(0, "a", "talking_head", framing="A"),
        _Beat(1, "b", "talking_head", framing="B"),
    ]
    wins = [(0.0, 1.0), (1.0, 4.0)]               # first is 1.0s < 2.2s
    nb, nw = merge_short_windows(beats, wins)
    assert nw == [(0.0, 4.0)]
    assert nb[0].framing == "B"                   # longer beat's fields win
    assert nb[0].index == 0


def test_merge_short_windows_never_merges_broll():
    beats = [
        _Beat(0, "a", "broll"),
        _Beat(1, "b", "broll"),
        _Beat(2, "c", "talking_head"),
    ]
    wins = [(0.0, 0.8), (0.8, 1.6), (1.6, 5.0)]   # short B-roll fast inserts
    nb, nw = merge_short_windows(beats, wins)
    assert nw == wins                             # untouched
    assert [b.shot_type for b in nb] == ["broll", "broll", "talking_head"]
    assert [b.index for b in nb] == [0, 1, 2]


def test_merge_short_windows_never_merges_reaction_beat():
    # A short reaction beat (silent hold, shot_type=talking_head) must survive
    # — it gets its real span from apply_holds later. Regression for
    # v2-20260614-001634, where the brief's lingering-scent reaction (0.9s) was
    # merged away in native mode and vanished from the final video.
    beats = [
        _Beat(0, "Hello there friend.", "talking_head"),
        _Beat(1, "", "talking_head", beat_kind="reaction", hold_s=0.9),
        _Beat(2, "And it works great.", "talking_head"),
    ]
    wins = [(0.0, 3.0), (3.0, 3.9), (3.9, 7.0)]   # middle is a short reaction
    nb, nw = merge_short_windows(beats, wins)
    assert nw == wins                              # untouched
    assert [b.beat_kind for b in nb] == ["speech", "reaction", "speech"]
    assert [b.index for b in nb] == [0, 1, 2]


def test_merge_short_windows_cascades_until_no_short_talking_head():
    """One merge can still be short — a 0.4s B-roll absorbing a 1.5s talking
    head leaves a 1.9s window, and a sub-2s reference slice is a hard EvoLink
    400 after every sibling clip was billed. Merging must repeat until no
    talking_head window is under min_s."""
    beats = [
        _Beat(0, "a", "broll"),
        _Beat(1, "b", "talking_head"),
        _Beat(2, "c", "broll"),
    ]
    wins = [(0.0, 0.4), (0.4, 1.9), (1.9, 6.0)]
    nb, nw = merge_short_windows(beats, wins)
    # First merge keeps the talking head (1.5s > 0.4s) at 1.9s — still short,
    # so it folds into the next B-roll (4.1s wins) on the second pass.
    assert nw == [(0.0, 6.0)]
    assert [b.shot_type for b in nb] == ["broll"]
    assert not any(
        b.shot_type == "talking_head" and (e - s) < 2.2
        for b, (s, e) in zip(nb, nw)
    )


def test_merge_short_windows_consecutive_shorts_keep_merging():
    beats = [
        _Beat(0, "a", "talking_head", framing="A"),
        _Beat(1, "b", "talking_head", framing="B"),
        _Beat(2, "c", "talking_head", framing="C"),
    ]
    wins = [(0.0, 0.9), (0.9, 1.8), (1.8, 6.0)]   # 0.9 + 0.9 is still < 2.2
    nb, nw = merge_short_windows(beats, wins)
    assert nw == [(0.0, 6.0)]
    assert nb[0].framing == "C"                   # longest beat's fields win
    assert nb[0].index == 0


def test_merge_short_windows_single_short_window_is_left_alone():
    beats = [_Beat(0, "a", "talking_head")]
    wins = [(0.0, 1.0)]
    nb, nw = merge_short_windows(beats, wins)
    assert nw == [(0.0, 1.0)]
    assert nb[0].shot_type == "talking_head"


class _FakeRun:
    """Records every subprocess.run command; used to assert the ffmpeg pass of
    apply_holds without invoking ffmpeg."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_apply_holds_noop_without_reaction_beats(tmp_path):
    """No reaction beat → inputs returned untouched, no ffmpeg pass."""
    al = _linear_alignment("Hello")
    beats = [_Beat(0, "Hello", "talking_head")]
    wins = [(0.0, 0.5)]
    vo, new_al, new_wins = apply_holds(
        beats, wins, al, "vo.wav", work_dir=tmp_path)
    assert vo == "vo.wav"
    assert new_al is al
    assert new_wins == wins
    assert not (tmp_path / "vo-holds.wav").exists()


def test_apply_holds_inserts_silence_and_shifts_alignment(tmp_path, monkeypatch):
    """A 1.0s reaction hold after the first speech beat: the reaction window is
    [prev_end, prev_end+hold], every char after the insertion shifts by exactly
    the hold, and total duration grows by Σhold_s. One ffmpeg splice pass."""
    import app.assembly as A
    fake = _FakeRun()
    monkeypatch.setattr(A.subprocess, "run", fake)
    # 'AB' 0.0-0.5, hold, 'CD' 1.0-1.5 (window 0.5-1.0 absorbed by the hold).
    al = Alignment(characters=list("ABCD"),
                   starts=[0.0, 0.25, 0.5, 0.75], ends=[0.25, 0.5, 0.75, 1.0])
    beats = [
        _Beat(0, "AB", "talking_head"),
        _Beat(1, "", "broll", beat_kind="reaction", hold_s=1.0,
              shows_creator=True),
        _Beat(2, "CD", "talking_head"),
    ]
    wins = [(0.0, 0.5), (0.5, 0.5), (0.5, 1.0)]

    vo, new_al, new_wins = apply_holds(
        beats, wins, al, "vo.wav", work_dir=tmp_path)

    assert vo == str(tmp_path / "vo-holds.wav")
    assert len(fake.calls) == 1                          # one splice pass
    fc = fake.calls[0][fake.calls[0].index("-filter_complex") + 1]
    assert "anullsrc=r=44100:cl=mono,atrim=duration=1.000" in fc
    assert "pcm_s16le" in fake.calls[0]
    # Reaction window == [prev_end, prev_end + hold].
    assert new_wins[1] == pytest.approx((0.5, 1.5))
    # Speech beat after the hold shifted by +1.0.
    assert new_wins[0] == pytest.approx((0.0, 0.5))
    assert new_wins[2] == pytest.approx((1.5, 2.0))
    # Chars at/after the insertion (prev_end=0.5) shift by +1.0; chars before
    # stay. Total duration += 1.0.
    assert new_al.starts == pytest.approx([0.0, 0.25, 1.5, 1.75])
    assert new_al.ends == pytest.approx([0.25, 0.5, 1.75, 2.0])
    assert new_al.duration == pytest.approx(al.duration + 1.0)
    assert new_al.characters == al.characters


def test_apply_holds_cumulative_over_two_reactions(tmp_path, monkeypatch):
    """Chars after the second hold shift by the SUM of both holds."""
    import app.assembly as A
    monkeypatch.setattr(A.subprocess, "run", _FakeRun())
    al = Alignment(characters=list("ABC"),
                   starts=[0.0, 1.0, 2.0], ends=[1.0, 2.0, 3.0])
    beats = [
        _Beat(0, "A", "talking_head"),
        _Beat(1, "", "broll", beat_kind="reaction", hold_s=0.5),
        _Beat(2, "B", "talking_head"),
        _Beat(3, "", "broll", beat_kind="reaction", hold_s=0.7),
        _Beat(4, "C", "talking_head"),
    ]
    wins = [(0.0, 1.0), (1.0, 1.0), (1.0, 2.0), (2.0, 2.0), (2.0, 3.0)]

    _, new_al, new_wins = apply_holds(
        beats, wins, al, "vo.wav", work_dir=tmp_path)

    # 'A' before any hold; 'B' after the first (+0.5); 'C' after both (+1.2).
    assert new_al.starts == pytest.approx([0.0, 1.5, 3.2])
    assert new_al.ends == pytest.approx([1.0, 2.5, 4.2])
    assert new_al.duration == pytest.approx(al.duration + 1.2)
    assert new_wins[1] == pytest.approx((1.0, 1.5))       # first hold
    assert new_wins[3] == pytest.approx((2.5, 3.2))       # second hold (shifted)
    assert new_wins[4] == pytest.approx((3.2, 4.2))


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_apply_holds_inserts_exact_silence_into_vo(tmp_path):
    """Real ffmpeg: a 1.0s hold splices 1.0s into a 2.0s VO → 3.0s output."""
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2.0",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("X" * 20)         # 2.0s VO
    beats = [
        _Beat(0, "X" * 10, "talking_head"),
        _Beat(1, "", "broll", beat_kind="reaction", hold_s=1.0),
        _Beat(2, "X" * 10, "talking_head"),
    ]
    wins = [(0.0, 1.0), (1.0, 1.0), (1.0, 2.0)]
    out, new_al, _ = apply_holds(beats, wins, al, str(vo), work_dir=tmp_path)
    assert _probe_duration(out) == pytest.approx(3.0, abs=0.1)
    assert new_al.duration == pytest.approx(3.0)


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_pad_audio_to_exact_duration(tmp_path):
    """pad_audio_to trails silence to EXACTLY target_s (r2v reference must
    equal the clip duration)."""
    src = tmp_path / "speech.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
         str(src)], check=True, capture_output=True,
    )
    dest = tmp_path / "padded.wav"
    pad_audio_to(src, 4.0, dest)
    assert _probe_duration(dest) == pytest.approx(4.0, abs=0.05)


@pytest.mark.asyncio
async def test_assemble_routes_reaction_beat_through_trim(tmp_path, monkeypatch):
    """A reaction beat is NEVER lipsynced/native-r2v even with shot_type
    talking_head — it routes through the silent trim path like B-roll."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)         # 2.0s VO
    beats = [
        _Beat(0, "X" * 10, "talking_head"),
        # Reaction beat carrying shot_type talking_head must NOT lipsync.
        _Beat(1, "", "talking_head", beat_kind="reaction", hold_s=1.0,
              shows_creator=True),
    ]
    wins = [(0.0, 1.0), (1.0, 2.0)]
    clips = [str(tmp_path / "clip0.mp4"), str(tmp_path / "clip1.mp4")]
    for c in clips:
        Path(c).touch()

    trims: list[str] = []

    def fake_probe(p):
        return al.duration if Path(p).name == "concat.mp4" else 4.0

    monkeypatch.setattr(A, "_probe_duration", fake_probe)
    monkeypatch.setattr(A, "slice_audio", lambda *a, **k: Path(a[-1]).touch())
    monkeypatch.setattr(A, "strip_audio",
                        lambda src, dest, duration=None, resolution=None, **k:
                        Path(dest).touch())
    monkeypatch.setattr(
        A, "trim_clip",
        lambda src, duration, dest, resolution=None, **k:
        (trims.append(Path(src).name), Path(dest).touch()))
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())

    class _Up:
        def upload(self, p):
            return f"https://fake/{Path(p).name}"

    class _Res:
        job_id, output_url = "job", "fake://synced"

    class _Lip:
        async def lipsync(self, *, video_url, audio_url, output_file_name, **kw):
            return _Res()

    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_Lip(), uploader=_Up(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
    )
    # Only beat 0 lipsynced; the reaction beat went through trim_clip.
    assert res.n_lipsynced == 1
    assert trims == ["clip1.mp4"]


def _probe_duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _count_frames(path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return int(out.stdout.strip())


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_trim_clip_exact_duration(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=4",
         "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True,
    )
    dest = tmp_path / "trim.mp4"
    trim_clip(src, 2.3, dest)
    assert dest.exists()
    assert _probe_duration(dest) == pytest.approx(2.3, abs=0.15)


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_strip_audio_duration_encodes_exact_frames(tmp_path):
    """duration=10/24 must encode EXACTLY 10 frames. Naive ms-rounding of the
    -t value (0.41(6) → \"0.417\") captures an 11th frame — ffmpeg keeps a
    frame iff pts < t — and every such beat pushes the concat one frame ahead
    of the VO."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "color=c=blue:s=720x1280:d=2:r=24", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    dest = tmp_path / "ten.mp4"
    strip_audio(src, dest, duration=10 / 24)
    assert _count_frames(dest) == 10
    # Without duration, the full clip passes through (48 frames = 2s @ 24fps).
    full = tmp_path / "full.mp4"
    strip_audio(src, full)
    assert _count_frames(full) == 48


def _probe_dims(path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
         str(path)], capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_encode_resolution_default_720_and_1080_optin(tmp_path):
    """720p (default) encodes 720x1280; resolution='1080p' encodes 1080x1920 —
    so a 1080p run is genuinely 1080p (no downscale back to 720)."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "color=c=blue:s=1080x1920:d=2:r=24", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    d720 = tmp_path / "d720.mp4"
    trim_clip(src, 1.0, d720)                       # default
    assert _probe_dims(d720) == (720, 1280)

    d1080 = tmp_path / "d1080.mp4"
    strip_audio(src, d1080, duration=1.0, resolution="1080p")
    assert _probe_dims(d1080) == (1080, 1920)


def _make_av_clip(path, *, seconds=2.0, size="720x1280", freq=440,
                  volume=0.5):
    """Colour video + sine audio test clip."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"color=c=blue:s={size}:d={seconds}",
         "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={seconds}",
         "-af", f"volume={volume}",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_master_audio_normalizes_to_target_lufs(tmp_path):
    """Two-pass loudnorm lands the master at −14 LUFS (±1) and 48kHz, from a
    deliberately quiet source."""
    src = tmp_path / "quiet.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "sine=frequency=300:duration=3", "-af", "volume=0.05",
         str(src)], check=True, capture_output=True,
    )
    out = tmp_path / "master.wav"
    master_audio(src, out, duration=3.0, room_tone=True)
    assert _probe_duration(out) == pytest.approx(3.0, abs=0.1)
    measure = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(out),
         "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    import json as _json
    import re as _re
    stats = _json.loads(_re.search(
        r"\{[^{}]*\"input_i\"[^{}]*\}", measure.stderr, _re.DOTALL).group(0))
    assert float(stats["input_i"]) == pytest.approx(-14.0, abs=1.0)
    rate = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    )
    assert rate.stdout.strip() == "48000"


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_finish_tags_bt709_muxes_master_and_keeps_duration(tmp_path):
    """The single finishing encode: BT.709 colour tags on the stream, the
    mastered audio muxed at 48kHz, duration preserved (drift invariant)."""
    from app.captions import Word, build_ass

    src = tmp_path / "src.mp4"
    _make_av_clip(src, seconds=2.0)
    mastered = tmp_path / "master.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=2", "-ar", "48000", str(mastered)],
        check=True, capture_output=True,
    )
    ass = tmp_path / "caps.ass"
    ass.write_text(build_ass([Word("test", 0.2, 1.2)], width=720, height=1280))
    out = tmp_path / "final.mp4"
    finish(src, mastered, out, duration=2.0, ass_path=ass, grade=True)
    assert _probe_duration(out) == pytest.approx(2.0, abs=0.15)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=color_space,color_transfer,color_primaries",
         "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "bt709,bt709,bt709"
    audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name,sample_rate", "-of", "csv=p=0",
         str(out)],
        check=True, capture_output=True, text=True,
    )
    assert audio.stdout.strip() == "aac,48000"


def test_head_accent_and_punch_in_filters():
    """Filter-string construction per transition style: frame-indexed settle
    for punch/slide (duration-preserving, scaled back to uniform dims),
    empty for plain cuts."""
    punch = head_accent_vf("punch", "720p")
    assert "min(n,5)" in punch and punch.endswith("scale=720:1280")
    slide = head_accent_vf("slide", "1080p")
    assert "1-n/3" in slide and "crop=w=1080:h=1920" in slide
    assert head_accent_vf("cut", "720p") == ""
    assert punch_in_vf("720p") == "crop=iw/1.12:ih/1.12,scale=720:1280"


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_trim_clip_extra_vf_preserves_dims_and_duration(tmp_path):
    """A head accent inside the trim must not change dims/duration — the
    concat demuxer's stream-copy invariants depend on both."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=2",
         "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True,
    )
    dest = tmp_path / "accent.mp4"
    trim_clip(src, 1.5, dest, resolution="720p",
              extra_vf=head_accent_vf("punch", "720p"))
    assert _probe_dims(dest) == (720, 1280)
    assert _probe_duration(dest) == pytest.approx(1.5, abs=0.15)


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
@pytest.mark.asyncio
async def test_assemble_raises_when_clip_shorter_than_window(tmp_path):
    """A clip shorter than its window must raise, not silently freeze/black-pad
    against the VO."""
    clip = tmp_path / "short.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=0.5",
         "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True,
    )
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.0",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("X" * 10)        # 1.0s VO
    beats = [_Beat(0, "X" * 10, "broll")]
    wins = [(0.0, 1.0)]                      # window needs 1.0s; clip is 0.5s
    with pytest.raises(RuntimeError, match="too short"):
        await assemble(
            beats=beats, windows=wins, clip_paths=[str(clip)],
            vo_audio_path=str(vo), alignment=al,
            lipsync_client=None, uploader=None,
            work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        )


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
@pytest.mark.asyncio
async def test_replicate_clip_shorter_than_window_but_covers_audio_is_ok(tmp_path):
    """heygen_replicate paces each beat off its ISOLATED per-beat audio, not the
    VO window. A clip shorter than its window but >= its audio-derived trim
    length is correctly sized — the trim-safety guard must NOT reject it (this
    reproduced the 'beat N: source clip is 4.04s but window needs 4.21s' crash)."""
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=0.6",
         "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True,
    )
    beat_audio = tmp_path / "beat0.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         str(beat_audio)], check=True, capture_output=True,
    )
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.0",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("X" * 10)                       # 1.0s VO
    beats = [_Beat(0, "X" * 10, "broll", voice_mode="background_vo")]
    wins = [(0.0, 1.0)]        # VO window 1.0s; clip 0.6s; audio-derived trim 0.5s
    # No lipsync (background_vo → attach_audio path); a local uploader stub is
    # unused on this branch. Must reach concat without raising "too short".
    out = await assemble(
        beats=beats, windows=wins, clip_paths=[str(clip)],
        vo_audio_path=str(vo), alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        isolated_audio=True,
        beat_audio_paths=[str(beat_audio)], beat_audio_durs=[0.5],
    )
    assert Path(out.path).exists()


@pytest.mark.asyncio
async def test_assemble_raises_on_large_av_drift(tmp_path, monkeypatch):
    """A >0.25s concat/VO mismatch must raise — every clip is already billed
    by then, but silently shipping a desynced ad is worse."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)         # 2.0s VO
    beats = [_Beat(0, "X" * 20, "broll")]
    wins = [(0.0, 2.0)]
    clip = tmp_path / "clip0.mp4"
    clip.touch()

    def fake_probe(p):
        if Path(p).name == "concat.mp4":
            return al.duration + 0.4          # broken invariant downstream
        return 4.0

    monkeypatch.setattr(A, "_probe_duration", fake_probe)
    monkeypatch.setattr(A, "trim_clip",
                        lambda src, duration, dest, resolution=None, **k:
                        Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())

    with pytest.raises(RuntimeError, match="A/V drift"):
        await assemble(
            beats=beats, windows=wins, clip_paths=[str(clip)],
            vo_audio_path="vo.mp3", alignment=al,
            lipsync_client=None, uploader=None,
            work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        )


@pytest.mark.asyncio
async def test_assemble_lipsyncs_full_clip_then_trims_output(tmp_path, monkeypatch):
    """Sync.so order: talking-head beats upload the FULL original clip (never
    a pre-trimmed stub), the VO slice spans min(clip duration, VO remainder),
    and the window trim happens ONCE — on the synced output, via strip_audio
    with an explicit duration."""
    import app.assembly as A

    al = _linear_alignment("X" * 30)         # 3.0s VO
    beats = [
        _Beat(0, "X" * 12, "talking_head"),
        _Beat(1, "X" * 18, "talking_head"),
    ]
    wins = [(0.0, 1.2), (1.2, 3.0)]
    clips = [str(tmp_path / "clip0.mp4"), str(tmp_path / "clip1.mp4")]
    for c in clips:
        Path(c).touch()
    src_durs = {"clip0.mp4": 2.0, "clip1.mp4": 4.0}

    def fake_probe(p):
        return src_durs.get(Path(p).name, al.duration)   # concat → no drift

    slices: list[tuple[float, float]] = []
    strips: list[tuple[str, float]] = []
    trims: list[str] = []

    def fake_slice(src, start, end, dest):
        slices.append((start, end))
        Path(dest).touch()

    def fake_strip(src, dest, duration=None, resolution=None, **k):
        strips.append((Path(src).name, duration))
        Path(dest).touch()

    def fake_trim(src, duration, dest, resolution=None, **k):
        trims.append(Path(src).name)
        Path(dest).touch()

    monkeypatch.setattr(A, "_probe_duration", fake_probe)
    monkeypatch.setattr(A, "slice_audio", fake_slice)
    monkeypatch.setattr(A, "strip_audio", fake_strip)
    monkeypatch.setattr(A, "trim_clip", fake_trim)
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())

    uploads: list[str] = []

    class _Up:
        def upload(self, p):
            uploads.append(Path(p).name)
            return f"https://fake/{Path(p).name}"

    class _Res:
        job_id, output_url = "job", "fake://synced"

    class _Lip:
        async def lipsync(self, *, video_url, audio_url, output_file_name, **kw):
            return _Res()

    assets: list[dict] = []
    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_Lip(), uploader=_Up(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        on_asset=lambda k, rec: assets.append(rec),
    )

    assert res.n_lipsynced == 2
    # The ORIGINAL clips were uploaded — no pre-trimmed stub, no trim_clip.
    assert {"clip0.mp4", "clip1.mp4"} <= set(uploads)
    assert not any("trim" in u for u in uploads)
    assert trims == []
    assert all(rec["source_local_path"] in clips for rec in assets)
    # Slice spans: beat 0 capped by its clip (2.0s < 3.0s of VO left);
    # beat 1 capped by the VO's end (4.0s clip, only 1.8s of VO left).
    slices.sort()
    assert slices[0] == pytest.approx((0.0, 2.0))
    assert slices[1] == pytest.approx((1.2, 3.0))
    # ONE re-encode trims the SYNCED output to the exact window length.
    strips.sort()
    assert strips[0][0] == "beat-0-synced.mp4"
    assert strips[0][1] == pytest.approx(1.2)
    assert strips[1][0] == "beat-1-synced.mp4"
    assert strips[1][1] == pytest.approx(1.8)


@pytest.mark.asyncio
async def test_assemble_whole_video_single_lipsync_pass(tmp_path, monkeypatch):
    """whole_video_lipsync=True: every beat is trimmed silently and concatenated,
    then ONE Sync.so pass runs over the whole cut + the FULL VO. The synced output
    carries the VO (no per-clip overlay), so n_lipsynced == 1, no per-clip slice is
    cut, and the original talking-head clips are never uploaded individually."""
    import app.assembly as A

    al = _linear_alignment("X" * 30)              # 3.0s VO
    beats = [
        _Beat(0, "X" * 10, "talking_head"),
        _Beat(1, "X" * 10, "broll"),
        _Beat(2, "X" * 10, "talking_head"),
    ]
    wins = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    clips = [str(tmp_path / f"clip{i}.mp4") for i in range(3)]
    for c in clips:
        Path(c).touch()

    trims: list[tuple[str, float]] = []
    slices: list = []
    overlays: list = []

    monkeypatch.setattr(A, "_probe_duration", lambda p: al.duration)  # no drift
    monkeypatch.setattr(
        A, "trim_clip",
        lambda src, duration, dest, resolution=None, **k: (
            trims.append((Path(src).name, duration)), Path(dest).touch()),
    )
    monkeypatch.setattr(A, "slice_audio", lambda *a, **k: slices.append(a))
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: (overlays.append(Path(src).name),
                                                Path(dest).touch()))
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())

    uploads: list[str] = []

    class _Up:
        def upload(self, p):
            uploads.append(Path(p).name)
            return f"https://fake/{Path(p).name}"

    class _Res:
        job_id, output_url = "whole-job", "fake://whole-synced"

    calls: list[dict] = []

    class _Lip:
        async def lipsync(self, *, video_url, audio_url, output_file_name, **kw):
            calls.append({"video_url": video_url, "audio_url": audio_url,
                          "name": output_file_name})
            return _Res()

    assets: list[dict] = []
    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_Lip(), uploader=_Up(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        whole_video_lipsync=True,
        on_asset=lambda k, rec: assets.append(rec),
    )

    # Exactly ONE lipsync call, over the concat + the full VO.
    assert len(calls) == 1
    assert calls[0]["name"] == "whole"
    assert calls[0]["video_url"].endswith("concat.mp4")
    assert calls[0]["audio_url"].endswith("vo.mp3")
    assert res.n_lipsynced == 1
    # Every beat trimmed silently; no per-clip slice; the SYNCED whole
    # (whose audio the one lipsync pass baked in) is what gets mastered.
    assert len(trims) == 3
    assert slices == []
    assert overlays == ["whole-synced.mp4"]
    # Original clips NEVER uploaded individually — only the concat + the VO.
    assert not any(name.startswith("clip") for name in uploads)
    assert set(uploads) == {"concat.mp4", "vo.mp3"}
    # The single whole-video job is recorded for resume.
    assert assets and assets[0].get("whole_video") is True
    assert assets[0]["job_id"] == "whole-job"


@pytest.mark.asyncio
async def test_assemble_whole_video_resumes_prior_pass(tmp_path, monkeypatch):
    """A resumed whole-video run reuses the already-paid Sync.so output instead
    of re-calling: no new lipsync call, no re-upload."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)              # 2.0s VO
    beats = [_Beat(0, "X" * 10, "talking_head"), _Beat(1, "X" * 10, "broll")]
    wins = [(0.0, 1.0), (1.0, 2.0)]
    clips = [str(tmp_path / f"clip{i}.mp4") for i in range(2)]
    for c in clips:
        Path(c).touch()
    prior_synced = tmp_path / "w" / "whole-synced.mp4"
    prior_synced.parent.mkdir(parents=True, exist_ok=True)
    prior_synced.touch()

    monkeypatch.setattr(A, "_probe_duration", lambda p: al.duration)
    monkeypatch.setattr(
        A, "trim_clip",
        lambda src, duration, dest, resolution=None, **k: Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())

    uploads: list[str] = []

    class _Up:
        def upload(self, p):
            uploads.append(Path(p).name)
            return "https://fake"

    class _Lip:
        async def lipsync(self, **k):
            raise AssertionError("must not re-call Sync.so on resume")

    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_Lip(), uploader=_Up(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        whole_video_lipsync=True,
        prior_lipsync_records=[{
            "index": -1, "whole_video": True, "job_id": "old",
            "synced_local_path": str(prior_synced),
        }],
    )

    assert res.n_lipsynced == 1
    assert uploads == []                          # nothing re-uploaded


def _patch_native_fakes(A, monkeypatch, *, src_dur, concat_dur, speech_end):
    """Stub every ffmpeg/ffprobe touchpoint of the native assemble() path,
    returning the call records. Source clips probe at ``src_dur``, the concat
    at ``concat_dur``; _detect_speech_end returns ``speech_end``."""
    calls: dict[str, list] = {
        "trim_keep": [], "trim": [], "slices": [], "attach": [],
        "attach_silence": [], "master": [],
    }

    def fake_probe(p):
        return concat_dur if Path(p).name == "concat.mp4" else src_dur

    monkeypatch.setattr(A, "_probe_duration", fake_probe)
    monkeypatch.setattr(
        A, "_probe_audio_streams",
        lambda p: {"audio_streams": 1, "audio_duration_s": src_dur - 0.1},
    )
    monkeypatch.setattr(A, "_detect_speech_end", lambda p: speech_end)
    monkeypatch.setattr(
        A, "trim_keep_audio",
        lambda src, duration, dest, resolution=None, **k: (calls["trim_keep"].append(
            (Path(src).name, duration)), Path(dest).touch()),
    )
    monkeypatch.setattr(
        A, "trim_clip",
        lambda src, duration, dest, resolution=None, **k: (calls["trim"].append(
            (Path(src).name, duration)), Path(dest).touch()),
    )
    monkeypatch.setattr(
        A, "slice_audio",
        lambda src, start, end, dest: (calls["slices"].append(
            (start, end)), Path(dest).touch()),
    )
    monkeypatch.setattr(
        A, "attach_audio",
        lambda video, audio, dest, duration: (calls["attach"].append(
            (Path(video).name, Path(audio).name, duration)),
            Path(dest).touch()),
    )
    monkeypatch.setattr(
        A, "attach_silence",
        lambda video, dest, duration, resolution=None: (
            calls["attach_silence"].append((Path(video).name, duration)),
            Path(dest).touch()),
    )
    # Finishing steps are stubbed: calls["master"] records WHICH source was
    # mastered (the concat itself in baked-audio modes, the VO in overlay
    # mode) — the structural invariant the old "no VO overlay" asserts kept.
    monkeypatch.setattr(
        A, "master_audio",
        lambda src, dest, **k: (calls["master"].append(Path(src).name),
                                Path(dest).touch()),
    )
    monkeypatch.setattr(
        A, "finish",
        lambda video, audio, dest, **k: Path(dest).touch(),
    )
    monkeypatch.setattr(
        A, "stitch",
        lambda paths, out: Path(out).write_bytes(b"CONCAT"),
    )
    return calls


@pytest.mark.asyncio
async def test_assemble_native_keeps_speech_and_attaches_broll_slice(
        tmp_path, monkeypatch):
    """native_lipsync=True: speaking beats (talking_head AND background_vo
    b-roll with dialogue) arrive already lipsynced with the re-rendered speech
    baked in — no Sync.so call, no upload — and are trimmed KEEPING their
    audio to speech_end + pad (1.3 + 0.12 > the 1.0s window). Silent beats
    (broll with no dialogue) get a synthetic silent aac track. No VO overlay:
    the concat IS the final, and duration_s is the probed concat duration."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)         # 2.0s VO
    # Beat 0: talking_head with dialogue → speaking → trim_keep_audio
    # Beat 1: broll with NO dialogue → silent beat → trim_clip + attach_silence
    beats = [_Beat(0, "X" * 10, "talking_head"), _Beat(1, "", "broll")]
    wins = [(0.0, 1.0), (1.0, 2.0)]
    clips = [str(tmp_path / "clip0.mp4"), str(tmp_path / "clip1.mp4")]
    for c in clips:
        Path(c).touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=2.42,
                                speech_end=1.3)

    manifest_updates: dict = {}

    class _Man:
        def update(self, **kw):
            manifest_updates.update(kw)

    out = tmp_path / "out.mp4"
    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al,
        lipsync_client=None, uploader=None,   # would crash if Sync.so path ran
        work_dir=tmp_path / "w", out_path=out,
        manifest=_Man(), native_lipsync=True,
    )

    assert res.n_lipsynced == 0
    # Talking head (speaking): trim KEEPING audio, to speech_end + 0.12 (beyond
    # the window).
    assert calls["trim_keep"] == [("clip0.mp4", pytest.approx(1.42))]
    # Silent broll: silent window trim then attach a synthetic silent aac track.
    # No VO slice cut, no attach_audio with a VO file.
    assert calls["trim"] == [("clip1.mp4", pytest.approx(1.0))]
    assert calls["slices"] == []
    assert calls["attach"] == []
    assert calls["attach_silence"] == [
        ("beat-1-silent.mp4", pytest.approx(1.0)),
    ]
    # No continuous-VO overlay — the concat's own mix is what gets
    # mastered and finished into the final.
    assert calls["master"] == ["concat.mp4"]
    assert out.exists()
    assert res.duration_s == pytest.approx(2.42)   # probed concat, not the VO
    # Manifest records only the speaking beat (beat 0) native audio probe.
    rec, = manifest_updates["native_audio"]
    assert rec["index"] == 0
    assert rec["source_local_path"] == clips[0]
    assert rec["speech_end"] == pytest.approx(1.3)
    assert rec["trim_end"] == pytest.approx(1.42)
    assert rec["audio_streams"] == 1
    assert rec["audio_duration_s"] == pytest.approx(3.9)


@pytest.mark.asyncio
@pytest.mark.parametrize("speech_end,src_dur,expected", [
    (1.3, 4.0, 1.42),    # speech past the window → extend to speech_end + pad
    (0.5, 4.0, 1.0),     # speech inside the window → keep the window
    (4.5, 4.0, 4.0),     # speech_end past the source → cap at src duration
    (None, 4.0, 1.0),    # probe failure → fall back to the window
])
async def test_assemble_native_speech_end_trim_clamps(
        tmp_path, monkeypatch, speech_end, src_dur, expected):
    """trim_end = min(src_dur, max(window_len, speech_end + 0.12)) — never
    under the window (the beat must fill its slot), never past the source."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)         # 1.0s VO
    beats = [_Beat(0, "X" * 10, "talking_head")]
    wins = [(0.0, 1.0)]
    clip = tmp_path / "clip0.mp4"
    clip.touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=src_dur,
                                concat_dur=expected, speech_end=speech_end)

    await assemble(
        beats=beats, windows=wins, clip_paths=[str(clip)],
        vo_audio_path="vo.mp3", alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        native_lipsync=True,
    )

    assert calls["trim_keep"] == [("clip0.mp4", pytest.approx(expected))]


@pytest.mark.asyncio
async def test_assemble_native_drift_logs_instead_of_raising(
        tmp_path, monkeypatch, capsys):
    """In native mode the VO-vs-video drift check is structurally meaningless
    (every clip carries its own audio; speech-end trims intentionally exceed
    the windows) — a deviation that would RAISE in Sync.so mode only logs."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)         # 2.0s VO
    beats = [_Beat(0, "X" * 20, "broll")]
    wins = [(0.0, 2.0)]
    clip = tmp_path / "clip0.mp4"
    clip.touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0,
                                concat_dur=al.duration + 0.4, speech_end=None)

    res = await assemble(
        beats=beats, windows=wins, clip_paths=[str(clip)],
        vo_audio_path="vo.mp3", alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        native_lipsync=True,
    )

    assert "native mode" in capsys.readouterr().err
    assert calls["master"] == ["concat.mp4"]
    assert res.duration_s == pytest.approx(al.duration + 0.4)


@pytest.mark.asyncio
async def test_assemble_native_raises_on_silent_talking_head(
        tmp_path, monkeypatch):
    """A native-mode talking head that arrives WITHOUT a baked audio stream
    means r2v dropped the reference speech — concatenating it would ship a
    mute mouth. assemble() must raise, AFTER recording the probe to the
    manifest so the paid clip stays recoverable."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)         # 1.0s VO
    beats = [_Beat(0, "X" * 10, "talking_head")]
    wins = [(0.0, 1.0)]
    clip = tmp_path / "clip0.mp4"
    clip.touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=1.0,
                                speech_end=None)
    monkeypatch.setattr(
        A, "_probe_audio_streams",
        lambda p: {"audio_streams": 0, "audio_duration_s": None},
    )

    manifest_updates: dict = {}

    class _Man:
        def update(self, **kw):
            manifest_updates.update(kw)

    with pytest.raises(RuntimeError, match="NO audio"):
        await assemble(
            beats=beats, windows=wins, clip_paths=[str(clip)],
            vo_audio_path="vo.mp3", alignment=al,
            lipsync_client=None, uploader=None,
            work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
            manifest=_Man(), native_lipsync=True,
        )

    assert calls["trim_keep"] == []          # raised before the trim
    rec, = manifest_updates["native_audio"]  # probe persisted for recovery
    assert rec["index"] == 0
    assert rec["audio_streams"] == 0


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
def test_detect_speech_end_real_audio(tmp_path):
    """silencedetect finds the start of a TRAILING silence run; audio that
    speaks to the end returns the full duration; a bad path returns None."""
    from app.assembly import _detect_speech_end

    trailing = tmp_path / "trailing.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=1.0", "-af", "apad", "-t", "2",
         str(trailing)], check=True, capture_output=True,
    )
    assert _detect_speech_end(trailing) == pytest.approx(1.0, abs=0.2)

    full = tmp_path / "full.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=2.0", str(full)],
        check=True, capture_output=True,
    )
    assert _detect_speech_end(full) == pytest.approx(2.0, abs=0.2)

    assert _detect_speech_end(tmp_path / "missing.wav") is None


# ---------------------------------------------------------------------------
# voice_mode authority: pipeline and assembly must agree (Fix 2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assemble_native_silent_voice_mode_overrides_dialogue(
        tmp_path, monkeypatch):
    """A beat with explicit voice_mode='silent' but non-empty dialogue_text
    must be treated as SILENT by assembly — it uses attach_silence (no audio
    stream), never trim_keep_audio.  This verifies that assembly honours the
    same resolve_voice_mode authority as the pipeline, so the two can never
    diverge on a contradictory beat and cause an unexpected RuntimeError or
    a mute-mouth clip."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)         # 1.0s VO
    # This beat has non-empty dialogue_text but voice_mode='silent' — the
    # explicit voice_mode wins.  Without Fix 2 the old heuristic (dialogue_text
    # non-empty → speaking) would have called trim_keep_audio and then hit the
    # "NO audio stream" RuntimeError for this clip.
    beats = [
        _Beat(0, "X" * 10, "broll", voice_mode="silent"),
    ]
    wins = [(0.0, 1.0)]
    clip = tmp_path / "clip0.mp4"
    clip.touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=1.0,
                                speech_end=None)

    await assemble(
        beats=beats, windows=wins, clip_paths=[str(clip)],
        vo_audio_path="vo.mp3", alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        native_lipsync=True,
    )

    # resolve_voice_mode sees voice_mode='silent' → attach_silence, not
    # trim_keep_audio.
    assert calls["trim_keep"] == [], (
        "trim_keep_audio must NOT be called for a silent voice_mode beat "
        "(Fix 2: assembly must defer to resolve_voice_mode, not dialogue_text)"
    )
    assert calls["attach_silence"], (
        "attach_silence must be called: the beat is silent, so it needs a "
        "synthetic silent aac track regardless of its dialogue_text"
    )


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
@pytest.mark.asyncio
async def test_assemble_native_end_to_end_concat_carries_audio(tmp_path):
    """Real-ffmpeg native run: the talking head keeps its baked speech past
    the window (tone to ~1.2s in a 1.0s window → ~1.32s trim), the silent
    B-roll (no dialogue) gets a synthetic silence track via attach_silence,
    and the stream-copy concat IS the final — with an audio track and no VO
    overlay pass."""
    th = tmp_path / "clip0.mp4"             # 3s video, tone 0-1.2s then silence
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=3:r=24",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
         "-af", "apad", "-t", "3", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "44100", "-ac", "2", str(th)],
        check=True, capture_output=True,
    )
    br = tmp_path / "clip1.mp4"             # silent B-roll (no dialogue → silent beat)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "color=c=green:s=720x1280:d=3:r=24", "-pix_fmt", "yuv420p", str(br)],
        check=True, capture_output=True,
    )
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=2.0",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("X" * 20)         # 2.0s VO
    # Beat 1 has empty dialogue → silent beat → attach_silence (not a VO slice).
    beats = [_Beat(0, "X" * 10, "talking_head"), _Beat(1, "", "broll")]
    wins = [(0.0, 1.0), (1.0, 2.0)]
    out = tmp_path / "final.mp4"

    res = await assemble(
        beats=beats, windows=wins, clip_paths=[str(th), str(br)],
        vo_audio_path=str(vo), alignment=al,
        lipsync_client=None, uploader=None,   # would crash if Sync.so path ran
        work_dir=tmp_path / "w", out_path=out,
        native_lipsync=True,
    )

    assert out.exists() and out.stat().st_size > 0
    assert res.n_lipsynced == 0
    # ~1.32s (speech-end trim) + 1.0s (B-roll window), NOT the 2.0s VO.
    assert res.duration_s == pytest.approx(2.32, abs=0.3)
    assert _probe_duration(out) == pytest.approx(res.duration_s, abs=0.15)
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert streams == ["aac"]                # the concat carries its own audio


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
@pytest.mark.asyncio
async def test_assemble_broll_only_end_to_end(tmp_path):
    """B-roll-only timeline needs no lipsync/upload — pure local ffmpeg path."""
    # Two silent clips.
    clips = []
    for i, color in enumerate(["red", "green"]):
        c = tmp_path / f"clip{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c={color}:s=720x1280:d=3", "-pix_fmt", "yuv420p",
             str(c)], check=True, capture_output=True,
        )
        clips.append(str(c))
    # A 1.1s VO tone.
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.1",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("Hello world")  # 1.1s
    beats = [_Beat(0, "Hello", "broll"), _Beat(1, " world", "broll")]
    wins = beat_windows(beats, al)
    out = tmp_path / "final.mp4"

    result = await assemble(
        beats=beats, windows=wins, clip_paths=clips,
        vo_audio_path=str(vo), alignment=al,
        lipsync_client=None, uploader=None,   # unused for broll-only
        work_dir=tmp_path / "work", out_path=out,
    )

    assert result.path == str(out)
    assert result.n_lipsynced == 0
    assert out.exists() and out.stat().st_size > 0
    # Final duration ≈ VO duration (sum of beat windows).
    assert _probe_duration(out) == pytest.approx(1.1, abs=0.3)


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
@pytest.mark.asyncio
async def test_assemble_parallel_lipsync_orders_and_persists(tmp_path, monkeypatch):
    """Talking-head beats lipsync CONCURRENTLY against the FULL original clip;
    output stays beat-ordered, each paid call is persisted to the manifest the
    moment it lands, and no captions are burned by default."""
    import app.assembly as A

    clips = []
    for i, color in enumerate(["red", "green", "blue"]):
        c = tmp_path / f"clip{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c={color}:s=720x1280:d=3", "-pix_fmt", "yuv420p",
             str(c)], check=True, capture_output=True,
        )
        clips.append(str(c))
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("AAAABBBBCCCC")   # 12 chars → 1.2s
    beats = [
        _Beat(0, "AAAA", "talking_head"),
        _Beat(1, "BBBB", "broll"),
        _Beat(2, "CCCC", "talking_head"),
    ]
    wins = beat_windows(beats, al)

    completion_order: list[int] = []

    class _Res:
        def __init__(self, jid, url):
            self.job_id, self.output_url = jid, url

    class _FakeLip:
        async def lipsync(self, *, video_url, audio_url, output_file_name, **kw):
            idx = int(output_file_name.split("-")[1])
            # Earlier beats sleep LONGER → if serial, order is [0,2];
            # if parallel, beat 2 finishes first → [2,0].
            await asyncio.sleep(0.15 if idx == 0 else 0.03)
            completion_order.append(idx)
            return _Res(f"job-{idx}", f"fake://{output_file_name}")

    uploads: list[str] = []

    class _FakeUp:
        def upload(self, p):
            uploads.append(Path(p).name)
            return f"https://fake/{Path(p).name}"

    monkeypatch.setattr(A, "_download", lambda url, dest: shutil.copy(clips[0], dest))

    lip_updates: list[dict] = []

    class _Man:
        def update(self, **kw):
            if "lipsync" in kw:
                lip_updates.append(kw["lipsync"])

    assets: list[tuple] = []
    out = tmp_path / "final.mp4"
    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path=str(vo),
        alignment=al, lipsync_client=_FakeLip(), uploader=_FakeUp(),
        work_dir=tmp_path / "work", out_path=out,
        on_asset=lambda k, p: assets.append((k, p)), manifest=_Man(),
        lipsync_concurrency=2,
    )

    assert res.n_lipsynced == 2
    assert out.exists() and out.stat().st_size > 0
    # The FULL original Seedance clips went to Sync.so, never a trimmed stub.
    assert {"clip0.mp4", "clip2.mp4"} <= set(uploads)
    assert not any(u.endswith("-trim.mp4") for u in uploads)
    # Concurrency allowed (2): beat 2 finished before beat 0 despite being
    # dispatched later.
    assert completion_order[0] == 2
    # Incremental persistence: manifest updated once per landed lipsync,
    # growing 1 → 2 (never lose a paid call to a later crash).
    assert [len(snap) for snap in lip_updates] == [1, 2]
    # No captions burned by default; on_asset fired per lipsynced beat.
    assert sum(1 for k, _ in assets if k == "lipsync_clip") == 2
    assert not (tmp_path / "work" / "captions.ass").exists()


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
@pytest.mark.asyncio
async def test_assemble_lipsync_concurrency_bounded(tmp_path, monkeypatch):
    """The default concurrency (1) serializes Sync.so calls so the plan-level
    concurrency cap is never exceeded (the 429 fix): dispatch order is preserved
    even though the first beat sleeps longest."""
    import app.assembly as A

    clips = []
    for i, color in enumerate(["red", "green", "blue"]):
        c = tmp_path / f"clip{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c={color}:s=720x1280:d=3", "-pix_fmt", "yuv420p",
             str(c)], check=True, capture_output=True,
        )
        clips.append(str(c))
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("AAAABBBBCCCC")
    beats = [_Beat(0, "AAAA", "talking_head"), _Beat(1, "BBBB", "broll"),
             _Beat(2, "CCCC", "talking_head")]
    wins = beat_windows(beats, al)

    in_flight = 0
    max_in_flight = 0
    completion_order: list[int] = []

    class _Res:
        def __init__(self, jid, url):
            self.job_id, self.output_url = jid, url

    class _FakeLip:
        async def lipsync(self, *, video_url, audio_url, output_file_name, **kw):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            idx = int(output_file_name.split("-")[1])
            await asyncio.sleep(0.15 if idx == 0 else 0.03)
            completion_order.append(idx)
            in_flight -= 1
            return _Res(f"job-{idx}", f"fake://{output_file_name}")

    class _FakeUp:
        def upload(self, p):
            return f"https://fake/{Path(p).name}"

    monkeypatch.setattr(A, "_download", lambda url, dest: shutil.copy(clips[0], dest))
    out = tmp_path / "final.mp4"
    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path=str(vo),
        alignment=al, lipsync_client=_FakeLip(), uploader=_FakeUp(),
        work_dir=tmp_path / "work", out_path=out,
        lipsync_concurrency=1,
    )
    assert res.n_lipsynced == 2
    # Never more than one Sync.so call in flight, and serial → ordered [0, 2].
    assert max_in_flight == 1
    assert completion_order == [0, 2]


@pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg/ffprobe required")
@pytest.mark.asyncio
async def test_assemble_resumes_prior_lipsync(tmp_path, monkeypatch):
    """A beat present in prior_lipsync_records reuses its already-paid Sync.so
    output (re-trimmed locally) instead of calling Sync.so again."""
    import app.assembly as A

    clips = []
    for i, color in enumerate(["red", "green", "blue"]):
        c = tmp_path / f"clip{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c={color}:s=720x1280:d=3", "-pix_fmt", "yuv420p",
             str(c)], check=True, capture_output=True,
        )
        clips.append(str(c))
    vo = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
         str(vo)], check=True, capture_output=True,
    )
    al = _linear_alignment("AAAABBBBCCCC")
    beats = [_Beat(0, "AAAA", "talking_head"), _Beat(1, "BBBB", "broll"),
             _Beat(2, "CCCC", "talking_head")]
    wins = beat_windows(beats, al)

    # Pre-stage a downloaded synced clip for beat 0 (already paid in a prior run).
    work = tmp_path / "work"
    work.mkdir()
    prior_synced = work / "beat-0-synced.mp4"
    shutil.copy(clips[0], prior_synced)
    prior = [{"index": 0, "job_id": "old-0",
              "syncso_url": "fake://old", "synced_local_path": str(prior_synced),
              "source_local_path": clips[0]}]

    called: list[int] = []

    class _Res:
        def __init__(self, jid, url):
            self.job_id, self.output_url = jid, url

    class _FakeLip:
        async def lipsync(self, *, video_url, audio_url, output_file_name, **kw):
            idx = int(output_file_name.split("-")[1])
            called.append(idx)
            return _Res(f"job-{idx}", f"fake://{output_file_name}")

    class _FakeUp:
        def upload(self, p):
            return f"https://fake/{Path(p).name}"

    monkeypatch.setattr(A, "_download", lambda url, dest: shutil.copy(clips[0], dest))
    out = tmp_path / "final.mp4"
    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path=str(vo),
        alignment=al, lipsync_client=_FakeLip(), uploader=_FakeUp(),
        work_dir=work, out_path=out,
        prior_lipsync_records=prior,
    )
    # Beat 0 reused (no Sync.so call); only beat 2 hit the API. Both counted.
    assert called == [2]
    assert res.n_lipsynced == 2
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.asyncio
async def test_assemble_native_background_vo_broll_keeps_native_audio(
        tmp_path, monkeypatch):
    """native_lipsync=True: a background_vo broll beat (broll shot_type WITH
    dialogue) is SPEAKING — it arrives with r2v-rendered speech baked in, just
    like an on_camera talking head. It must use trim_keep_audio (keep its own
    audio), never attach_audio with a VO slice; the concat's own mix
    is the mastered source."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)         # 2.0s VO
    # Beat 0: on_camera talking_head, Beat 1: background_vo broll (has dialogue)
    beats = [
        _Beat(0, "X" * 10, "talking_head"),
        _Beat(1, "X" * 10, "broll"),         # non-empty dialogue → speaking
    ]
    wins = [(0.0, 1.0), (1.0, 2.0)]
    clips = [str(tmp_path / "clip0.mp4"), str(tmp_path / "clip1.mp4")]
    for c in clips:
        Path(c).touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=2.2,
                                speech_end=0.9)

    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        native_lipsync=True,
    )

    assert res.n_lipsynced == 0
    # BOTH beats are speaking → both use trim_keep_audio.
    assert len(calls["trim_keep"]) == 2
    assert calls["trim_keep"][0][0] == "clip0.mp4"
    assert calls["trim_keep"][1][0] == "clip1.mp4"
    # No VO slices cut, no attach_audio with a VO file, no overlay.
    assert calls["slices"] == []
    assert calls["attach"] == []
    assert calls["attach_silence"] == []
    assert calls["master"] == ["concat.mp4"]
    # Manifest records both beats as native audio.
    assert len(calls["trim"]) == 0


@pytest.mark.asyncio
async def test_assemble_native_silent_beat_gets_silence_not_vo_slice(
        tmp_path, monkeypatch):
    """native_lipsync=True: a silent beat (reaction hold or broll with no
    dialogue) must receive a synthetic silent aac track via attach_silence —
    NOT the ElevenLabs VO slice via attach_audio. The ElevenLabs oracle must
    never appear in the output."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)         # 2.0s VO
    beats = [
        _Beat(0, "X" * 10, "talking_head"),
        # Silent broll: empty dialogue → not speaking → attach_silence
        _Beat(1, "", "broll"),
        # Reaction hold: beat_kind="reaction" → is_reaction → attach_silence
        _Beat(2, "", "talking_head", beat_kind="reaction", hold_s=0.5),
    ]
    wins = [(0.0, 1.0), (1.0, 1.5), (1.5, 2.0)]
    clips = [str(tmp_path / f"clip{i}.mp4") for i in range(3)]
    for c in clips:
        Path(c).touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=2.0,
                                speech_end=0.9)

    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        native_lipsync=True,
    )

    assert res.n_lipsynced == 0
    # Only the talking_head (beat 0) is speaking → trim_keep_audio.
    assert calls["trim_keep"] == [("clip0.mp4", pytest.approx(1.02))]
    # Silent beats: trim_clip then attach_silence (no VO slice).
    assert len(calls["trim"]) == 2
    assert calls["trim"][0][0] == "clip1.mp4"
    assert calls["trim"][1][0] == "clip2.mp4"
    assert len(calls["attach_silence"]) == 2
    assert calls["attach_silence"][0] == ("beat-1-silent.mp4", pytest.approx(0.5))
    assert calls["attach_silence"][1] == ("beat-2-silent.mp4", pytest.approx(0.5))
    # Critically: no ElevenLabs VO slice was ever cut or attached.
    assert calls["slices"] == []
    assert calls["attach"] == []
    assert calls["master"] == ["concat.mp4"]


@pytest.mark.asyncio
async def test_assemble_native_ambient_keeps_a_silent_beats_own_audio(
        tmp_path, monkeypatch):
    """native_ambient_audio=True (an end-to-end generator that authors audio for
    EVERY clip, silent beats included — their briefs name the room tone): those
    beats must keep their own track. Overwriting it with synthetic silence
    throws away sound we paid to render and opens an audible dropout at the cut
    between two beats that do carry room tone."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)
    beats = [
        _Beat(0, "X" * 10, "talking_head"),
        _Beat(1, "", "broll"),
        _Beat(2, "", "talking_head", beat_kind="reaction", hold_s=0.5),
    ]
    wins = [(0.0, 1.0), (1.0, 1.5), (1.5, 2.0)]
    clips = [str(tmp_path / f"clip{i}.mp4") for i in range(3)]
    for c in clips:
        Path(c).touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=2.0,
                               speech_end=0.9)

    await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path=None,
        alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        native_lipsync=True, native_ambient_audio=True,
    )

    # Every beat kept its own audio — none went through trim_clip+attach_silence.
    assert [c[0] for c in calls["trim_keep"]] == [
        "clip0.mp4", "clip1.mp4", "clip2.mp4"]
    assert calls["trim"] == []
    assert calls["attach_silence"] == []
    # Silent beats are cut to their window (only speaking beats trim to speech).
    assert calls["trim_keep"][1][1] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_assemble_native_ambient_falls_back_to_silence_on_a_mute_clip(
        tmp_path, monkeypatch):
    """The flag is a promise about the generator, not proof about the file: a
    clip that came back with no audio track still needs the synthetic one, or
    the concat demuxer hits a stream mismatch mid-timeline."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)
    beats = [_Beat(0, "X" * 10, "talking_head"), _Beat(1, "", "broll")]
    wins = [(0.0, 1.0), (1.0, 1.5)]
    clips = [str(tmp_path / f"clip{i}.mp4") for i in range(2)]
    for c in clips:
        Path(c).touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=1.5,
                               speech_end=0.9)
    # The silent beat's clip probes as having NO audio stream.
    monkeypatch.setattr(
        A, "_probe_audio_streams",
        lambda p: {"audio_streams": 0 if Path(p).name == "clip1.mp4" else 1,
                   "audio_duration_s": 3.9},
    )

    await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path=None,
        alignment=al,
        lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        native_lipsync=True, native_ambient_audio=True,
    )

    assert [c[0] for c in calls["trim_keep"]] == ["clip0.mp4"]
    assert [c[0] for c in calls["trim"]] == ["clip1.mp4"]
    assert len(calls["attach_silence"]) == 1


# ---------------------------------------------------------------------------
# heygen_replicate assembly routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_heygen_replicate_on_camera_calls_lipsync(
    tmp_path, monkeypatch,
):
    """on_camera beat under heygen_replicate → lipsync call with padded audio;
    background_vo → attach_audio with padded audio;
    silent → attach_silence; NO global VO overlay."""
    import app.assembly as A

    al = _linear_alignment("X" * 30)   # 3.0s VO
    beats = [
        _Beat(0, "X" * 10, "talking_head"),          # on_camera → lipsync
        _Beat(1, "X" * 10, "broll", voice_mode="background_vo"),  # attach_audio
        _Beat(2, "", "broll"),                        # silent → attach_silence
    ]
    wins = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    clips = [str(tmp_path / f"clip{i}.mp4") for i in range(3)]
    for c in clips:
        Path(c).touch()

    # Per-beat audio: only beats 0+1 speak (beat 2 is silent → None).
    b_audio_paths = [str(tmp_path / "b0.mp3"),
                     str(tmp_path / "b1.mp3"), None]
    for p in b_audio_paths[:2]:
        Path(p).write_bytes(b"")
    b_audio_durs = [1.5, 1.5, None]

    lipsync_calls: list = []
    attach_audio_calls: list = []
    attach_silence_calls: list = []
    pad_calls: list = []
    trim_calls: list = []
    trim_keep_calls: list = []

    class _FakeLipClient:
        video_needs_audio = True

        async def lipsync(self, *, video_url, audio_url, **kw):
            lipsync_calls.append((video_url, audio_url))

            class _Res:
                job_id, output_url = "job", "https://fake/synced.mp4"
            return _Res()

    def fake_probe(p):
        return 4.0 if Path(p).name != "concat.mp4" else al.duration

    def fake_pad(src, target, dest):
        pad_calls.append((Path(src).name, target))
        Path(dest).write_bytes(b"")

    def fake_trim(src, dur, dest, resolution=None, **k):
        trim_calls.append((Path(src).name, dur))
        Path(dest).write_bytes(b"")

    def fake_trim_keep(src, dur, dest, resolution=None, **k):
        trim_keep_calls.append((Path(src).name, dur))
        Path(dest).write_bytes(b"")

    def fake_attach_audio(video, audio, dest, duration):
        attach_audio_calls.append((Path(video).name, Path(audio).name, duration))
        Path(dest).write_bytes(b"")

    def fake_attach_silence(video, dest, duration, resolution=None):
        attach_silence_calls.append((Path(video).name, duration))
        Path(dest).write_bytes(b"")

    monkeypatch.setattr(A, "_probe_duration", fake_probe)
    monkeypatch.setattr(A, "pad_audio_to", fake_pad)
    monkeypatch.setattr(A, "trim_clip", fake_trim)
    monkeypatch.setattr(A, "trim_keep_audio", fake_trim_keep)
    monkeypatch.setattr(A, "attach_audio", fake_attach_audio)
    monkeypatch.setattr(A, "attach_silence", fake_attach_silence)
    monkeypatch.setattr(A, "slice_audio", lambda *a, **k: Path(a[-1]).touch())
    monkeypatch.setattr(A, "strip_audio",
                        lambda src, dest, duration=None, resolution=None, **k:
                        Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    mastered_srcs: list[str] = []
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: (mastered_srcs.append(Path(src).name),
                                                Path(dest).touch()))
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())

    class _FakeUp:
        def upload(self, p):
            return f"https://cdn/{Path(p).name}"

    res = await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_FakeLipClient(), uploader=_FakeUp(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        isolated_audio=True,
        beat_audio_paths=b_audio_paths,
        beat_audio_durs=b_audio_durs,
    )

    # 1 on_camera beat → 1 lipsync call (Replicate HeyGen).
    assert len(lipsync_calls) == 1
    assert res.n_lipsynced == 1

    # on_camera: the SILENT i2v clip is muxed with its padded audio BEFORE
    # upload (HeyGen validates the input video has an audio stream), so the
    # lipsync video_url is the muxed clip; the padded audio is the audio_url.
    assert lipsync_calls[0][0].endswith("beat-0-muxed.mp4")
    assert lipsync_calls[0][1].endswith("beat-0-audio-padded.wav")

    # attach_audio is called twice: the on_camera mux (clip + beat-0 audio) and
    # the background_vo track (beat-1 silent clip + beat-1 audio). Both use the
    # PADDED audio (the ≤15% HeyGen duration constraint applies to both).
    assert len(attach_audio_calls) == 2
    assert [c for c in attach_audio_calls if c[1] == "beat-0-audio-padded.wav"]
    assert [c for c in attach_audio_calls if c[1] == "beat-1-audio-padded.wav"]

    # silent beat: attach_silence (no audio).
    assert len(attach_silence_calls) >= 1

    # No global VO overlay (audio_baked_in=True for isolated_audio): the
    # concat's own per-beat mix is the mastered source, never the VO.
    assert mastered_srcs == ["concat.mp4"]


@pytest.mark.asyncio
async def test_assemble_heygen_replicate_pads_audio_to_clip_duration(
    tmp_path, monkeypatch,
):
    """The audio is padded to max(4, ceil(audio_dur)) before being passed to
    the Replicate lipsync client — audio shorter than the 4s Seedance floor
    must be padded to 4s so the <15% duration mismatch constraint is met."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)   # 1.0s VO
    beats = [_Beat(0, "X" * 10, "talking_head")]
    wins = [(0.0, 1.0)]
    clip = str(tmp_path / "clip0.mp4")
    Path(clip).touch()

    # Beat audio is only 1.0s — clip was sized to max(4, ceil(1.0)) = 4s.
    b_audio = str(tmp_path / "b0.mp3")
    Path(b_audio).write_bytes(b"")
    b_audio_durs = [1.0]

    pad_targets: list = []

    class _FakeLipClient:
        video_needs_audio = True

        async def lipsync(self, *, video_url, audio_url, **kw):
            class _Res:
                job_id, output_url = "j", "https://fake/s.mp4"
            return _Res()

    def fake_pad(src, target, dest):
        pad_targets.append(target)
        Path(dest).write_bytes(b"")

    monkeypatch.setattr(A, "_probe_duration", lambda p: 4.0)
    monkeypatch.setattr(A, "pad_audio_to", fake_pad)
    monkeypatch.setattr(A, "attach_audio",
                        lambda video, audio, dest, dur: Path(dest).touch())
    monkeypatch.setattr(A, "trim_clip", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "trim_keep_audio",
                        lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "attach_silence",
                        lambda video, dest, dur, resolution=None: Path(dest).touch())
    monkeypatch.setattr(A, "slice_audio", lambda *a, **k: Path(a[-1]).touch())
    monkeypatch.setattr(A, "strip_audio",
                        lambda src, dest, duration=None, resolution=None, **k:
                        Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())

    class _FakeUp:
        def upload(self, p):
            return f"https://cdn/{Path(p).name}"

    await assemble(
        beats=beats, windows=wins, clip_paths=[clip], vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_FakeLipClient(), uploader=_FakeUp(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        isolated_audio=True,
        beat_audio_paths=[b_audio],
        beat_audio_durs=b_audio_durs,
    )

    # The pad call's target must be 4.0 (Seedance floor / max(4, ceil(1.0))).
    assert any(abs(t - 4.0) < 0.01 for t in pad_targets), \
        f"expected pad target ~4.0, got {pad_targets}"


@pytest.mark.asyncio
async def test_assemble_isolated_trims_trailing_dead_air_to_speech_end(
    tmp_path, monkeypatch,
):
    """On-camera isolated beats must trim the SYNCED clip to speech_end + a
    small tail pad, NOT the full ceil'd clip_dur — otherwise a short line
    (e.g. 2.3s) sits in a 4s clip with ~1.7s of silent, still-animating mouth
    (reads as dead-air pacing + lips-moving-without-audio)."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)   # 1.0s VO
    beats = [_Beat(0, "X" * 10, "talking_head")]
    wins = [(0.0, 1.0)]
    clip = str(tmp_path / "clip0.mp4")
    Path(clip).touch()
    b_audio = str(tmp_path / "b0.mp3")
    Path(b_audio).write_bytes(b"")

    trim_keep_calls: list = []

    class _FakeSyncClient:
        async def lipsync(self, *, video_url, audio_url, **kw):
            class _Res:
                job_id, output_url = "job", "https://fake/synced.mp4"
            return _Res()

    monkeypatch.setattr(A, "_probe_duration", lambda p: 4.0)
    # Speech ends at 2.3s inside the clip; clip_dur = max(4, ceil(2.3)) = 4.
    monkeypatch.setattr(A, "_detect_speech_end", lambda p: 2.3)
    monkeypatch.setattr(A, "pad_audio_to",
                        lambda src, target, dest: Path(dest).write_bytes(b""))
    monkeypatch.setattr(A, "attach_audio",
                        lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "trim_clip", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(
        A, "trim_keep_audio",
        lambda src, dur, dest, resolution=None, **k: (
            trim_keep_calls.append(dur), Path(dest).touch()))
    monkeypatch.setattr(A, "attach_silence",
                        lambda video, dest, dur, resolution=None: Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())

    class _FakeUp:
        def upload(self, p):
            return f"https://cdn/{Path(p).name}"

    await assemble(
        beats=beats, windows=wins, clip_paths=[clip], vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=_FakeSyncClient(), uploader=_FakeUp(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        isolated_audio=True,
        beat_audio_paths=[b_audio], beat_audio_durs=[2.3],
    )

    # speech_end 2.3 + tail 0.30 = 2.60, which is < clip_dur 4 → trim to 2.60.
    assert trim_keep_calls, "trim_keep_audio was not called for the on_camera beat"
    assert abs(trim_keep_calls[-1] - 2.60) < 0.01, \
        f"expected trim to speech_end+0.30 ≈ 2.60, got {trim_keep_calls}"


@pytest.mark.parametrize("speech_end,clip_dur,exp_len,exp_fade", [
    # Normal case: the 0.30s pad fits, so the click-guard fade sits in silence.
    (2.3, 4.0, 2.60, _NATIVE_TAIL_FADE_S),
    # Exact fit: a 4.95s line in a 5s clip — the cap eats the pad, so the fade
    # must shrink to the 0.05s of headroom instead of ramping the last consonant.
    (4.95, 5.0, 5.00, 0.05),
    # No headroom at all → no fade.
    (5.0, 5.0, 5.00, 0.0),
])
def test_isolated_trim_never_fades_over_real_speech(
        monkeypatch, speech_end, clip_dur, exp_len, exp_fade):
    """The tail fade is a click guard for the silence AFTER the line. The
    isolated path caps trim_len at the generated clip length, so on an exact-fit
    beat the fade would otherwise ramp real words down to nothing."""
    import app.assembly as A
    monkeypatch.setattr(A, "_detect_speech_end", lambda p: speech_end)
    trim_len, fade = A._isolated_trim("b0.mp3", clip_dur)
    assert trim_len == pytest.approx(exp_len)
    assert fade == pytest.approx(exp_fade)
    # Whatever the fade is, it must start at or after the end of speech.
    assert trim_len - fade >= speech_end - 1e-9


@pytest.mark.asyncio
async def test_assemble_syncso_isolated_uploads_silent_clip_no_mux(
    tmp_path, monkeypatch,
):
    """Sync.so accepts silent video + separate audio (no video_needs_audio
    attr, unlike the HeyGen dub models) → the on_camera beat uploads the RAW
    silent clip and the PADDED isolated audio; attach_audio (the HeyGen mux)
    is never called for it."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)   # 1.0s VO
    beats = [_Beat(0, "X" * 10, "talking_head")]
    wins = [(0.0, 1.0)]
    clip = str(tmp_path / "clip0.mp4")
    Path(clip).touch()
    b_audio = str(tmp_path / "b0.mp3")
    Path(b_audio).write_bytes(b"")

    lipsync_calls: list = []
    attach_audio_calls: list = []
    pad_calls: list = []

    class _FakeSyncClient:  # NO video_needs_audio attr, like SyncSoLipsync
        async def lipsync(self, *, video_url, audio_url, **kw):
            lipsync_calls.append((video_url, audio_url))

            class _Res:
                job_id, output_url = "job", "https://fake/synced.mp4"
            return _Res()

    def fake_pad(src, target, dest):
        pad_calls.append(target)
        Path(dest).write_bytes(b"")

    def fake_attach_audio(video, audio, dest, duration):
        attach_audio_calls.append((Path(video).name, Path(audio).name))
        Path(dest).write_bytes(b"")

    monkeypatch.setattr(A, "_probe_duration", lambda p: 4.0)
    monkeypatch.setattr(A, "pad_audio_to", fake_pad)
    monkeypatch.setattr(A, "attach_audio", fake_attach_audio)
    monkeypatch.setattr(A, "trim_clip", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "trim_keep_audio", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "attach_silence",
                        lambda video, dest, dur, resolution=None: Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())
    monkeypatch.setattr(A, "master_audio",
                        lambda src, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())

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
    assert video_url.endswith("clip0.mp4")       # raw silent clip, NOT *-muxed
    assert audio_url.endswith("beat-0-audio-padded.wav")
    assert attach_audio_calls == []              # no HeyGen mux for sync.so
    assert pad_calls == [4.0]                    # padded to max(4, ceil(1.0))
    assert res.n_lipsynced == 1


@pytest.mark.asyncio
async def test_assemble_heygen_replicate_no_global_overlay(
    tmp_path, monkeypatch,
):
    """heygen_replicate sets audio_baked_in=True → the continuous VO is never
    the mastered source; the concat's own per-beat mix feeds the finishing."""
    import app.assembly as A

    al = _linear_alignment("X" * 10)
    beats = [_Beat(0, "", "broll")]        # silent beat — simplest possible
    wins = [(0.0, 1.0)]
    clip = str(tmp_path / "clip0.mp4")
    Path(clip).touch()

    mastered_srcs: list[str] = []

    def record_overlay(src, dest, **k):
        mastered_srcs.append(Path(src).name)
        Path(dest).touch()

    monkeypatch.setattr(A, "_probe_duration", lambda p: 4.0)
    monkeypatch.setattr(A, "pad_audio_to", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "trim_clip", lambda *a, **k: Path(a[2]).touch())
    monkeypatch.setattr(A, "attach_silence",
                        lambda video, dest, dur, resolution=None: Path(dest).touch())
    monkeypatch.setattr(A, "slice_audio", lambda *a, **k: Path(a[-1]).touch())
    monkeypatch.setattr(A, "strip_audio",
                        lambda src, dest, duration=None, resolution=None, **k:
                        Path(dest).touch())
    monkeypatch.setattr(A, "stitch", lambda paths, out: Path(out).touch())
    monkeypatch.setattr(A, "master_audio", record_overlay)
    monkeypatch.setattr(A, "finish",
                        lambda video, audio, dest, **k: Path(dest).touch())
    monkeypatch.setattr(A, "_download", lambda url, dest: Path(dest).touch())

    class _FakeUp:
        def upload(self, p):
            return f"https://cdn/{Path(p).name}"

    await assemble(
        beats=beats, windows=wins, clip_paths=[clip], vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=None, uploader=_FakeUp(),
        work_dir=tmp_path / "w", out_path=tmp_path / "out.mp4",
        isolated_audio=True,
        beat_audio_paths=[None],
        beat_audio_durs=[None],
    )

    assert mastered_srcs == ["concat.mp4"], (
        "in heygen_replicate mode the concat's own mix must be mastered — "
        "never the continuous VO"
    )


@pytest.mark.asyncio
async def test_assemble_native_captions_via_forced_alignment(
        tmp_path, monkeypatch):
    """Native mode has no trustworthy TTS timing — captions must come from
    force-aligning the KNOWN script against the assembled audio, and the
    aligned words end up in the burned ASS."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)
    beats = [_Beat(0, "hello there friend", "talking_head"),
             _Beat(1, "", "broll")]
    wins = [(0.0, 1.0), (1.0, 2.0)]
    clips = [str(tmp_path / "clip0.mp4"), str(tmp_path / "clip1.mp4")]
    for c in clips:
        Path(c).touch()
    calls = _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=2.4,
                                speech_end=1.3)

    aligned_against: list[tuple[str, str]] = []

    async def fake_force_align(*, media_path, text):
        aligned_against.append((Path(media_path).name, text))
        return _linear_alignment("hello there friend")

    out = tmp_path / "out.mp4"
    await assemble(
        beats=beats, windows=wins, clip_paths=clips, vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=out,
        native_lipsync=True, burn_captions=True,
        force_align=fake_force_align,
    )

    # Aligned the CONCAT (the audio that actually plays) with the spoken text.
    assert aligned_against == [("concat.mp4", "hello there friend")]
    ass = (tmp_path / "w" / "captions.ass").read_text()
    assert "hello" in ass and "friend" in ass
    assert calls["master"] == ["concat.mp4"]


@pytest.mark.asyncio
async def test_assemble_native_captions_skip_on_alignment_failure(
        tmp_path, monkeypatch, capsys):
    """Forced-alignment failure must skip captions (mistimed text is worse
    than none) — never fail the paid run."""
    import app.assembly as A

    al = _linear_alignment("X" * 20)
    beats = [_Beat(0, "hello there friend", "talking_head")]
    wins = [(0.0, 2.0)]
    clip = str(tmp_path / "clip0.mp4")
    Path(clip).touch()
    _patch_native_fakes(A, monkeypatch, src_dur=4.0, concat_dur=2.0,
                        speech_end=1.3)

    async def broken_force_align(*, media_path, text):
        raise RuntimeError("alignment API down")

    out = tmp_path / "out.mp4"
    res = await assemble(
        beats=beats, windows=wins, clip_paths=[clip], vo_audio_path="vo.mp3",
        alignment=al, lipsync_client=None, uploader=None,
        work_dir=tmp_path / "w", out_path=out,
        native_lipsync=True, burn_captions=True,
        force_align=broken_force_align,
    )

    assert res.n_beats == 1
    assert not (tmp_path / "w" / "captions.ass").exists()
    assert "forced alignment failed" in capsys.readouterr().err


def test_trim_keep_audio_adds_a_tail_fade(monkeypatch, tmp_path):
    """Native r2v narration clips end with an abrupt click (vendor-documented;
    their prescribed fix is a volume envelope). finish() fades only the FINAL
    master, so the fade has to ride each native clip's own re-encode."""
    import app.assembly as A
    seen = {}
    monkeypatch.setattr(A, "_run", lambda cmd: seen.setdefault("cmd", cmd))
    A.trim_keep_audio(tmp_path / "in.mp4", 5.0, tmp_path / "out.mp4")
    cmd = seen["cmd"]
    assert "-af" in cmd
    af = cmd[cmd.index("-af") + 1]
    # fade OUT, ending exactly at the clip end
    assert af.startswith("afade=t=out:")
    assert "st=4.920" in af and "d=0.080" in af


def test_trim_keep_audio_skips_fade_on_a_clip_shorter_than_the_fade(
        monkeypatch, tmp_path):
    """Nothing sensible to ramp — and a negative start time would be an ffmpeg
    error on a paid render."""
    import app.assembly as A
    seen = {}
    monkeypatch.setattr(A, "_run", lambda cmd: seen.setdefault("cmd", cmd))
    A.trim_keep_audio(tmp_path / "in.mp4", 0.05, tmp_path / "out.mp4")
    assert "-af" not in seen["cmd"]
