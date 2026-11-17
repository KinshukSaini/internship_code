"""Burned-in word-pop captions from ElevenLabs character alignment.

No transcription needed — we already know the exact script and have
character-level timing from ElevenLabs. Group chars into words, emit an ASS
file with one Dialogue event per 1-3 word chunk that scale-pops in on the
word onset (the dominant short-form ad style), burn with ffmpeg's subtitles
filter.

Style (user-directed, 2026-07-10, matched to a reference frame): small
yellow ITALIC serif (Playfair Display), natural sentence case, soft dark
edge, centred at ~72% of frame height. Each 1-3 word chunk FADES in/out
(\\fad) — no scale-pop, which read as "popping up weird". Voice-pacing
punctuation (em-dashes, ellipses) is display-stripped. Layout units are real
output pixels: PlayResX/PlayResY are set to the encode dims, so sizing holds
at 720p and 1080p alike.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.voice import Alignment

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"

# ElevenLabs v3 audio tags ([excited], [sighs deeply], …) are delivery
# directions, not speech, but their characters still appear in the alignment
# — they must never reach a burned caption. Tags can span several tokens, so
# they're skipped as bracketed CHARACTER spans, not filtered as words.

# A token is an emphasis keyword if it carries a digit or currency/percent
# sign (prices, discounts, counts) — product-name terms are matched via the
# caller-supplied emphasis_terms.
_EMPHASIS_CHAR_RE = re.compile(r"[0-9%$₹€£]")
_TOKEN_STRIP = ".,!?;:'\"()"


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


def words_from_alignment(alignment: Alignment) -> list[Word]:
    """Group per-character timing into per-word timing (split on whitespace).

    Audio-tag tokens like ``[excited]`` are dropped — their time span is
    simply skipped (no caption shows while the tag is voiced).
    """
    words: list[Word] = []
    cur = ""
    cur_start: float | None = None
    cur_end: float | None = None
    in_tag = False
    for ch, s, e in zip(alignment.characters, alignment.starts, alignment.ends):
        if in_tag:
            in_tag = ch != "]"
            continue
        if ch == "[":
            in_tag = True
            continue
        if ch.isspace():
            if cur:
                words.append(Word(cur, cur_start or 0.0, cur_end or 0.0))
                cur, cur_start, cur_end = "", None, None
            continue
        if not cur:
            cur_start = s
        cur += ch
        cur_end = e
    if cur:
        words.append(Word(cur, cur_start or 0.0, cur_end or 0.0))
    return words


def words_for_beats(beat_alignments: list[Alignment | None],
                    offsets: list[float]) -> list[Word]:
    """Concatenate per-beat word timings onto the assembled timeline.

    Isolated-audio mode's final video is per-beat TTS clips concatenated
    back-to-back — the continuous-VO alignment does NOT match it. Each beat's
    words are exact within its own audio, so shifting them by the beat clip's
    start offset in the concat reconstructs the true timeline. Silent beats
    pass ``None`` and contribute nothing.
    """
    words: list[Word] = []
    for alignment, offset in zip(beat_alignments, offsets):
        if alignment is None:
            continue
        words.extend(
            Word(w.text, w.start + offset, w.end + offset)
            for w in words_from_alignment(alignment)
        )
    return words


def style_for(width: int, height: int) -> dict:
    """Derive the caption style from the encode dims (real-pixel units).

    Matched to the user's reference frame: warm-yellow italic serif, small
    (≈4.2% of frame height), thin dark edge + soft shadow (no heavy stroke),
    sitting centre-frame at ~72% height ("3/4th position").
    """
    return {
        "font": "Playfair Display",
        "size": round(0.042 * height),           # 54 @720p, 81 @1080p
        "primary": "&H0030C2F2",                 # warm yellow (ASS &HAABBGGRR)
        "accent": "&H0030C2F2",                  # uniform — no keyword colour
        "outline_colour": "&H00101010",
        "outline": max(1, round(0.0012 * height)),   # thin edge, 2 @1280
        "shadow": round(0.0025 * height),        # soft drop shadow
        "margin_v": round(0.28 * height),        # y ≈ 0.72 * height
        # Side margins bound the smart-wrap width (WrapStyle 0): a chunk of
        # long words can exceed the frame, and libass will NOT wrap
        # \pos-anchored text without margin room — it overflows.
        "margin_h": round(0.055 * width),        # 40 @720p, 59 @1080p
        "play_res_x": width,
        "play_res_y": height,
    }


def _ass_time(t: float) -> str:
    """Seconds → ASS H:MM:SS.cc (centiseconds)."""
    cs_total = int(round(max(0.0, t) * 100))
    h, cs_total = divmod(cs_total, 360000)
    m, cs_total = divmod(cs_total, 6000)
    s, cs = divmod(cs_total, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _is_emphasis(token: str, terms: frozenset[str]) -> bool:
    if _EMPHASIS_CHAR_RE.search(token):
        return True
    return token.strip(_TOKEN_STRIP).lower() in terms


def _clean(text: str) -> str:
    """Normalise a token for DISPLAY. The script text is written for the
    VOICE, not the screen: em-dashes/ellipses are pacing marks, CAPS words
    are prosody emphasis, and sentence punctuation lands mid-chunk once
    words are regrouped 3-at-a-time. Captions therefore render lowercase
    (standalone "i" → "I"), with pacing marks and trailing full stops/commas
    stripped (?/! survive — they carry meaning). Braces would open ASS
    override blocks, so they're dropped too."""
    for ch in ("{", "}", "—", "–", "…"):
        text = text.replace(ch, "")
    text = text.strip().lower().rstrip(".,")
    return "I" if text == "i" else text


# Smooth entrance/exit: a quick fade (no scale animation — the pop read as
# glitchy). Out-fade overlaps the next chunk's in-fade for a soft handoff.
_POP = r"\fad(120,80)"

_LAST_CHUNK_HOLD = 0.3


def build_ass(words: list[Word], *, width: int, height: int,
              words_per_line: int = 3,
              emphasis_terms: tuple[str, ...] = (),
              total_duration: float | None = None,
              style: dict | None = None) -> str:
    """Build an ASS document: pop-in caption chunks + optional hook/CTA.

    Each chunk of ``words_per_line`` words is one Dialogue event, visible
    from its first word's onset until the next chunk's onset (no flicker
    gap); the last chunk holds 0.3s past its final word, capped at
    ``total_duration``.
    """
    st = {**style_for(width, height), **(style or {})}
    terms = frozenset(
        t.strip(_TOKEN_STRIP).lower() for t in emphasis_terms if t.strip()
    )
    fields = (
        "Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding"
    )

    def style_line(name: str, size: int, primary: str) -> str:
        return (
            f"Style: {name},{st['font']},{size},{primary},{st['primary']},"
            f"{st['outline_colour']},&H00000000,-1,-1,0,0,100,100,0,0,1,"
            f"{st['outline']},{st['shadow']},2,{st['margin_h']},"
            f"{st['margin_h']},{st['margin_v']},1"
        )

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {st['play_res_x']}\n"
        f"PlayResY: {st['play_res_y']}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        f"Format: Name, {fields}\n"
        f"{style_line('Pop', st['size'], st['primary'])}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    def dialogue(style_name: str, start: float, end: float, y: int,
                 text: str) -> str:
        pos = rf"\an5\pos({st['play_res_x'] // 2},{y})"
        return (f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},"
                f"{style_name},,0,0,0,,{{{pos}{_POP}}}{text}")

    caption_y = st["play_res_y"] - st["margin_v"]
    accent_on = rf"{{\1c{st['accent'].replace('&H00', '&H', 1)}&}}"
    accent_off = rf"{{\1c{st['primary'].replace('&H00', '&H', 1)}&}}"

    lines: list[str] = []
    chunks = [words[i:i + words_per_line]
              for i in range(0, len(words), words_per_line)]
    for ci, group in enumerate(chunks):
        if not group:
            continue
        start = group[0].start
        if ci + 1 < len(chunks):
            end = chunks[ci + 1][0].start
        else:
            end = group[-1].end + _LAST_CHUNK_HOLD
            if total_duration is not None:
                end = min(end, total_duration)
        end = max(end, start + 0.01)
        parts = []
        for w in group:
            token = _clean(w.text)
            if not token:
                continue          # a bare pacing mark ("—") displays as nothing
            if _is_emphasis(token, terms):
                token = f"{accent_on}{token}{accent_off}"
            parts.append(token)
        if not parts:
            continue
        lines.append(dialogue("Pop", start, end, caption_y, " ".join(parts)))

    return header + "\n".join(lines) + "\n"


@lru_cache(maxsize=1)
def ffmpeg_has_subtitles_filter() -> bool:
    """Whether the ffmpeg on PATH was built with libass (the `subtitles`
    filter). The 2026 homebrew-core formula dropped libass, so a stock
    `brew install ffmpeg` CANNOT burn captions — probing up front turns an
    opaque filtergraph parse error at the end of a paid run into a clean
    skip-with-warning (assembly.finish) or an actionable error (burn)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, check=True,
        )
    except Exception:  # noqa: BLE001 — no ffmpeg at all; let callers fail there
        return False
    return any(
        line.split()[1:2] == ["subtitles"] for line in out.stdout.splitlines()
    )


def subtitles_filter(ass_path: str | Path,
                     fonts_dir: str | Path | None = None) -> str:
    """The ffmpeg subtitles filter string for libass.

    ``fonts_dir`` must be a path with NO Windows drive letter (i.e. relative).
    FFmpeg's filter-graph parser uses ':' as an option separator, so an
    absolute Windows path like 'C:\\...' breaks the filter regardless of
    escaping — the parser consumes 'C' as an option name and '\\:' as the
    separator.  Pass a relative path (e.g. inside the job's work_dir) to
    avoid this entirely.  Falls back to FONTS_DIR when not given.
    """
    chosen = Path(fonts_dir) if fonts_dir is not None else FONTS_DIR

    def esc(p: str | Path) -> str:
        # Forward slashes work on Windows in FFmpeg/libass and have no special
        # meaning in the filter graph, so only ':' needs escaping.
        return str(p).replace("\\", "/").replace(":", "\\:")

    return f"subtitles={esc(ass_path)}:fontsdir={esc(chosen)}"


def burn(video_path: str | Path, ass_path: str | Path, out_path: str | Path) -> None:
    """Burn an ASS subtitle file onto a video via ffmpeg's subtitles filter.

    Standalone re-encode kept for tooling/tests; the pipeline burns captions
    inside assembly's single finishing pass instead.
    """
    if not ffmpeg_has_subtitles_filter():
        raise RuntimeError(
            "this ffmpeg build has no `subtitles` filter (built without "
            "libass) — captions cannot be burned. Install a libass-capable "
            "ffmpeg and point FFMPEG_DIR at its directory."
        )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-vf", subtitles_filter(ass_path),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-c:a", "copy", str(out)],
        check=True, capture_output=True,
    )
