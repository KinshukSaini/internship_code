import shutil

import pytest

from app.captions import (
    Word,
    build_ass,
    style_for,
    subtitles_filter,
    words_for_beats,
    words_from_alignment,
)
from app.voice import Alignment


def test_words_from_alignment_groups_on_whitespace():
    al = Alignment(
        characters=list("Hi yo!"),
        starts=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
        ends=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
    )
    words = words_from_alignment(al)
    assert [w.text for w in words] == ["Hi", "yo!"]
    assert words[0].start == 0.0 and words[0].end == 0.2
    assert words[1].start == 0.3 and words[1].end == 0.6


def test_words_handles_leading_trailing_space():
    al = Alignment(
        characters=list(" a  b "),
        starts=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
        ends=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
    )
    words = words_from_alignment(al)
    assert [w.text for w in words] == ["a", "b"]


def test_words_drops_audio_tags():
    """ElevenLabs v3 audio tags voiced as delivery directions must never
    reach a burned caption."""
    text = "[excited] wow [sighs deeply] ok"
    n = len(text)
    al = Alignment(
        characters=list(text),
        starts=[i * 0.1 for i in range(n)],
        ends=[(i + 1) * 0.1 for i in range(n)],
    )
    words = words_from_alignment(al)
    assert [w.text for w in words] == ["wow", "ok"]


def test_words_for_beats_offsets_each_beat():
    al = Alignment(
        characters=list("hi yo"),
        starts=[0.0, 0.1, 0.2, 0.3, 0.4],
        ends=[0.1, 0.2, 0.3, 0.4, 0.5],
    )
    words = words_for_beats([al, None, al], [0.0, 2.0, 5.0])
    assert [w.text for w in words] == ["hi", "yo", "hi", "yo"]
    assert words[0].start == 0.0
    assert words[2].start == pytest.approx(5.0)
    assert words[3].end == pytest.approx(5.5)


def test_style_scales_with_resolution():
    s720 = style_for(720, 1280)
    s1080 = style_for(1080, 1920)
    assert s720["play_res_x"] == 720 and s720["play_res_y"] == 1280
    assert s1080["play_res_x"] == 1080 and s1080["play_res_y"] == 1920
    # Small elegant serif (~4.2% of height), per the user's reference frame.
    assert s720["size"] == 54 and s1080["size"] == 81
    # Thin edge, not a heavy stroke.
    assert s1080["outline"] == 2
    # y ≈ 0.72 * height ("3/4th position").
    assert s1080["margin_v"] == 538


def test_build_ass_pop_events():
    words = [
        Word("hello", 0.0, 0.4),
        Word("there", 0.5, 0.9),
        Word("friend", 1.0, 1.6),
    ]
    ass = build_ass(words, width=1080, height=1920, words_per_line=2)
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    # Small yellow ITALIC serif, natural case (user reference, 2026-07-10).
    assert "Style: Pop,Playfair Display,81,&H0030C2F2" in ass
    assert ",-1,-1,0,0," in ass          # Bold + Italic flags
    # Smooth fade, no scale-pop / karaoke wipe animation.
    assert "\\kf" not in ass and "\\fscx" not in ass
    assert "\\fad(120,80)" in ass
    assert "\\an5" in ass
    assert "hello there" in ass and "friend" in ass
    assert "HELLO" not in ass            # sentence case, never CAPS
    # 3 words at 2/line → 2 Dialogue events.
    assert ass.count("Dialogue:") == 2


def test_build_ass_chunk_visibility_no_gap():
    """A chunk stays visible until the NEXT chunk's onset; the last chunk
    holds 0.3s, capped at total_duration."""
    words = [Word("a", 0.0, 0.4), Word("b", 1.0, 1.4)]
    ass = build_ass(words, width=720, height=1280, words_per_line=1,
                    total_duration=1.5)
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    # First chunk: 0.0 → 1.0 (next onset), not 0.4.
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,0:00:01.00")
    # Last chunk: 1.4 + 0.3 hold capped at 1.5.
    assert lines[1].startswith("Dialogue: 0,0:00:01.00,0:00:01.50")


def test_build_ass_uniform_colour_no_visible_emphasis():
    """The reference style is uniform yellow — the emphasis wrap still runs
    but accent == primary, so no word renders a different colour."""
    words = [Word("only", 0.0, 0.3), Word("₹499", 0.4, 0.8)]
    ass = build_ass(words, width=720, height=1280,
                    emphasis_terms=("GlowSerum",))
    assert "₹499" in ass
    assert ass.count("&H30C2F2") >= 0   # accent == primary yellow
    assert "&H00FFFF&" not in ass       # the old white/yellow scheme is gone


def test_build_ass_strips_voice_pacing_marks():
    """Em-dashes/ellipses are prosody marks for the TTS, not caption copy;
    a bare mark token disappears entirely."""
    words = [Word("wow—", 0.0, 0.4), Word("—", 0.5, 0.6),
             Word("really…", 0.7, 1.2)]
    ass = build_ass(words, width=720, height=1280, words_per_line=1)
    assert "—" not in ass and "…" not in ass
    assert "wow" in ass and "really" in ass
    # The bare "—" chunk produced no Dialogue event at all.
    assert ass.count("Dialogue:") == 2


def test_build_ass_no_overlay_styles():
    """Hook/CTA overlays were removed (user: ugly) — captions only."""
    ass = build_ass([Word("hi", 0.0, 0.5)], width=720, height=1280,
                    total_duration=20.0)
    assert "Hook" not in ass and "CTA" not in ass
    assert ass.count("\nStyle: ") == 1   # ("WrapStyle: 0" would false-hit a bare count)


def test_build_ass_strips_braces():
    ass = build_ass([Word("a{b}c", 0.0, 0.5)], width=720, height=1280)
    assert "abc" in ass


def test_build_ass_normalizes_display_casing():
    """Script text is written for the VOICE (CAPS = prosody emphasis,
    sentence capitals land mid-chunk after regrouping) — display is
    lowercase, standalone i → I, trailing full stops/commas stripped,
    ?/! kept."""
    words = [
        Word("fragrances.", 0.0, 0.3),
        Word("And", 0.4, 0.6),        # sentence-initial capital mid-chunk
        Word("it", 0.7, 0.8),
        Word("ACTUALLY", 0.9, 1.3),   # prosody CAPS
        Word("works!", 1.4, 1.8),
        Word("I", 1.9, 2.0),
        Word("promise?", 2.1, 2.4),
    ]
    ass = build_ass(words, width=720, height=1280, words_per_line=4)
    dialogue = "\n".join(ln for ln in ass.splitlines()
                          if ln.startswith("Dialogue:"))
    assert "fragrances and it actually" in dialogue
    assert "works! I promise?" in dialogue
    assert "And" not in dialogue and "ACTUALLY" not in dialogue
    assert "fragrances." not in dialogue   # trailing full stop stripped


def test_build_ass_empty_words():
    ass = build_ass([], width=720, height=1280)
    assert "[Events]" in ass
    assert ass.count("Dialogue:") == 0


def test_subtitles_filter_escapes_and_points_at_fonts():
    f = subtitles_filter("/tmp/x/caps.ass")
    assert f.startswith("subtitles=/tmp/x/caps.ass:fontsdir=")
    assert "fonts" in f


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_burn_produces_output(tmp_path):
    import subprocess
    from app.captions import burn, ffmpeg_has_subtitles_filter

    if not ffmpeg_has_subtitles_filter():
        pytest.skip("ffmpeg on this machine lacks libass (no subtitles "
                    "filter) — burn correctly refuses; see FFMPEG_DIR")

    # Generate a 1s test video (solid colour, silent).
    vid = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=gray:s=720x1280:d=1",
         "-pix_fmt", "yuv420p", str(vid)],
        check=True, capture_output=True,
    )
    ass = tmp_path / "caps.ass"
    ass.write_text(build_ass([Word("test", 0.0, 0.8)], width=720, height=1280))
    out = tmp_path / "out.mp4"
    burn(vid, ass, out)
    assert out.exists() and out.stat().st_size > 0
