"""ElevenLabs voiceover generation with character-level timestamps.

The whole script is voiced in ONE call (eleven_v3 needs longer context for
stable prosody, and a single VO track is the master clock for the rest of
the pipeline). The character-level alignment returned alongside the audio is
the single source of truth for both (a) mapping timeline beats to time
windows and (b) building captions — no Whisper needed.

Also exposes the curated voice catalog (6 voice_ids per gender, picked by
vibe). `describe_character` chooses a `voice_vibe` from the brief; the
pipeline resolves to a voice_id via `voice_for(gender, vibe)`. Voice
selections from the catalog are sourced from the ElevenLabs shared library
ranked by community clone-count (the strongest quality signal).
"""
from __future__ import annotations

import asyncio
import base64
import re
import subprocess as _subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app import costs
from app.config import (
    CARTESIA_API_VERSION,
    CARTESIA_MODEL,
    CARTESIA_SPEED,
    ELEVENLABS_MODEL,
    ELEVENLABS_STABILITY,
    USE_PRONUNCIATION_FIX,
    FISH_API_BASE,
    FISH_MODEL,
    FISH_SPEED,
    VOICE_PROVIDER,
    Settings,
)

# ── Script-fidelity helpers ──────────────────────────────────────────────────

_PARALINGUISTIC = re.compile(r"[\(\[][^)\]]*[\)\]]")  # (sighs), [laughs]


def _words(text: str) -> set[str]:
    cleaned = _PARALINGUISTIC.sub(" ", text or "").lower()
    return {w for w in re.split(r"[^a-z0-9\u0900-\u097f]+", cleaned) if w}


def script_fidelity(transcript: str, expected: str) -> float:
    """Normalized word-overlap (Jaccard) of transcript vs expected line.

    Strips bracketed/parenthetical paralinguistics the native model adds
    ((sighs), [laughs], (birds chirping)). Empty `expected` (silent beats)
    scores 1.0 — there is no line to violate. Devanagari range included so
    Hindi lines score too.
    """
    we = _words(expected)
    if not we:
        return 1.0
    wt = _words(transcript)
    if not wt:
        return 0.0
    return len(we & wt) / len(we | wt)


# ── Audio-extraction helper (used by transcribe) ─────────────────────────────

def _extract_audio(src, dst) -> None:
    """16k mono wav for STT."""
    _subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         str(dst)], check=True, capture_output=True,
    )


def _audio_duration(path) -> float:
    """Seconds of audio, for metering Scribe (which bills by duration).

    Best-effort: a failed probe must never break transcription, so an
    unreadable file bills as 0 rather than raising. Kept local instead of
    importing assembly's probe — assembly imports this module.
    """
    try:
        out = _subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            check=True, capture_output=True, text=True,
        )
        return float(out.stdout.strip())
    except (_subprocess.CalledProcessError, ValueError, OSError):
        return 0.0

# eleven_v3 does not support SSML <break/> — it would speak the tag literally.
_SSML_BREAK_RE = re.compile(r"<break\b[^>]*/?>", re.I)

# Delivery speed for eleven_v3 (VoiceSettings.speed). 1.0 is the API default and
# read ~187 wpm on our Indian-English IVCs — draggy for punchy Gen-Z UGC. 1.15
# lands ~215 wpm (in the confident-creator range) without sounding rushed; the
# upper safe bound is ~1.2. Tune here if delivery reads too fast/slow.
_VO_SPEED = 1.15


# ── Per-provider text prep ───────────────────────────────────────────────────
# The planner writes prosody INTO the script, in eleven_v3's dialect: inline
# [audio tags], ONE ALL-CAPS word per sentence for emphasis, and … / — pacing
# (app/gemini.py). Only ElevenLabs speaks that dialect. Sent raw to another
# provider the markup is read LITERALLY, so each provider gets the same intent
# translated into its own controls rather than a stripped-flat script.
#
# Safe to do: assembly.beat_windows derives the master clock text from
# alignment.characters (already tag-free on ElevenLabs, since the API consumes
# the tags) and matches beats via _strip_tags(dialogue_text) — so a tag-stripped
# or tag-rewritten script produces an alignment of exactly the shape the
# ElevenLabs path already produces. Downstream timing is unaffected.

_AUDIO_TAG_RE = re.compile(r"\[\s*([^\]\n]{1,40}?)\s*\]")

# eleven_v3 tag → Fish s2.1-pro tag. Fish uses the SAME [bracket] syntax with
# open-ended natural-language values, so the planner's expressiveness survives a
# near-1:1 rename instead of being thrown away. Keys are the tags the planner is
# allowed to emit (gemini.py: [excited] [laughs] [giggles] [happy gasp] [sighs]),
# plus close variants. NOTE: s1 uses (parentheses) with a fixed catalogue — never
# mix the two syntaxes.
_FISH_TAG_MAP = {
    "excited": "excited",
    "laughs": "laughing",
    "laugh": "laughing",
    "laughing": "laughing",
    "giggles": "chuckling",
    "chuckles": "chuckling",
    "happy gasp": "gasping",
    "gasps": "gasping",
    "sighs": "sighing",
    "sigh": "sighing",
    "whispers": "whispering",
    "shouts": "shouting",
    "sarcastic": "sarcastic",
    "curious": "curious",
    "happy": "happy",
    "sad": "sad",
    "confident": "confident",
}

# voice_vibe → Cartesia generation_config.emotion (57-value enum). Cartesia has
# no inline tag syntax we can map per-sentence, so expressiveness is set ONCE per
# generation from the vibe the character description already picked.
# Cartesia's PRIMARY emotions (most training data, most reliable) are
# neutral/calm/angry/content/sad/scared — this map leans on those where the vibe
# allows, because emotion is a weak lever on instant clones (see below).
_CARTESIA_EMOTION_BY_VIBE = {
    "upbeat_lively": "excited",
    "warm_relatable": "content",
    "sassy_expressive": "confident",
    "youthful_excited": "enthusiastic",
    "professional_clear": "neutral",
    "soothing_narrative": "calm",
    "crisp_informative": "neutral",
    "mature_authentic": "content",
}

# All-caps emphasis runs the planner emits (INSANE, LITERALLY, OBSESSED). The
# 4-char floor deliberately spares short real acronyms (UV, SPF, AC, PM) where
# letter-by-letter is the CORRECT reading. A 4+-letter acronym (GRWM) is the
# known false positive — rare enough to accept, and only affects non-EL providers.
_CAPS_WORD_RE = re.compile(r"\b([A-Z]{4,})\b")


def _collapse_ws(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", text or "").strip()


def strip_audio_tags(text: str) -> str:
    """Drop every [audio tag] — for providers with no inline tag vocabulary.

    Normalises whitespace, because this feeds a TTS request (clean text in).
    Do NOT use this for the alignment text — see spoken_text().
    """
    return _collapse_ws(_AUDIO_TAG_RE.sub(" ", text or ""))


# Deliberately assembly._strip_tags' EXACT pattern: tags removed, nothing else
# touched — no whitespace collapsing, no case changes.
_ASSEMBLY_TAG_RE = re.compile(r"\[[^\]]*\]")


def spoken_text(text: str) -> str:
    """The exact character sequence the audio speaks — the ALIGNMENT text.

    This must be byte-identical to what assembly._strip_tags produces from the
    same string, because assembly.beat_windows locates each beat by
    ``align_text.find(_strip_tags(beat.dialogue_text))``. Tag removal leaves the
    surrounding spaces in place ("[excited] hi" → " hi"), so collapsing or
    stripping whitespace here would shift every character index and make that
    find() miss — after which the cursor fallback silently drifts each cut.

    test_spoken_text_matches_assembly_strip_tags pins the two together.
    """
    return _ASSEMBLY_TAG_RE.sub("", text or "")


def decaps_emphasis(text: str) -> str:
    """Lowercase ALL-CAPS emphasis words.

    Cartesia is explicit that capitalization is counterproductive: all-caps
    tokens are read LETTER BY LETTER ("this is INSANE" → "this is I-N-S-A-N-E"),
    and it changes how a word is read rather than how loud it sounds. Left in,
    this reads as a voice-quality problem when it is really a text-prep bug.
    """
    return _CAPS_WORD_RE.sub(lambda m: m.group(1).lower(), text or "")


def prep_for_cartesia(text: str) -> str:
    """Cartesia sonic-3.5: no inline tags, no CAPS. Punctuation carries pacing."""
    return _collapse_ws(decaps_emphasis(strip_audio_tags(text)))


def prep_for_fish(text: str) -> str:
    """Fish s2.1-pro: keep expressiveness, translated into Fish's own tags.

    Known eleven_v3 tags are renamed to their Fish equivalent; unknown tags are
    dropped rather than guessed (an unmapped tag would be spoken as text).
    CAPS emphasis becomes Fish's [emphasis] marker, which the docs say goes
    immediately before the target word.
    """
    def _tag(m: re.Match) -> str:
        mapped = _FISH_TAG_MAP.get(m.group(1).strip().lower())
        return f"[{mapped}]" if mapped else " "

    out = _AUDIO_TAG_RE.sub(_tag, text or "")
    out = _CAPS_WORD_RE.sub(lambda m: f"[emphasis] {m.group(1).lower()}", out)
    return _collapse_ws(out)


def cartesia_emotion_for_vibe(vibe: str | None) -> str:
    """voice_vibe → Cartesia emotion enum value. Unknown vibe → 'neutral'."""
    return _CARTESIA_EMOTION_BY_VIBE.get((vibe or "").strip(), "neutral")


# ── Pronunciation repair ─────────────────────────────────────────────────────
# The planner emits per-run {word: respelling} pairs (gemini.Pronunciation).
# How each engine consumes them differs, and the difference is entirely about
# whether the MASTER CLOCK sees the respelling:
#
#   ElevenLabs → an `alias` pronunciation-dictionary rule. VERIFIED 2026-07-29:
#       the substitution happens server-side and the returned alignment keeps the
#       ORIGINAL spelling (probe: 'Ayurvedic' span 0.507s -> 1.040s while
#       alignment.characters stayed "This Ayurvedic oil actually worked."). So the
#       clock is untouched. NEVER substitute locally on this path — the alignment
#       would then carry the respelling and beat_windows' find() would miss.
#   Cartesia / Fish → substituted locally into the provider text. Safe because
#       AlignedVoice force-aligns against spoken_text(script), which keeps the
#       original spelling.
#   Seedance native → substituted into the quoted line in the video prompt
#       (app/pipeline.py). Captions are unaffected: they come from dialogue_text.


def apply_respellings(text: str, pronunciations: Any) -> str:
    """Swap each `word` for its `respelling` in `text` (whole words only).

    Case-insensitive matching so a sentence-initial "Ayurvedic" is caught too.
    Accepts Pronunciation models, dicts, or (word, respelling) pairs so callers
    need not import the planner's schema.
    """
    if not pronunciations:
        return text or ""
    out = text or ""
    for p in pronunciations:
        word = _get(p, "word") or (p[0] if isinstance(p, (tuple, list)) else None)
        respell = _get(p, "respelling") or (
            p[1] if isinstance(p, (tuple, list)) and len(p) > 1 else None)
        if not word or not respell:
            continue
        out = re.sub(rf"\b{re.escape(str(word))}\b", str(respell), out,
                     flags=re.I)
    return out


def alias_rules(pronunciations: Any) -> list[dict]:
    """Planner pronunciations → ElevenLabs `alias` dictionary rules.

    Alias (not `phoneme`) is deliberate: alias works on EVERY model, is a plain
    pre-inference text substitution so we know exactly what reaches the model,
    and needs no IPA. Inline <phoneme> SSML does not work on eleven_v3 at all,
    and inline /IPA/ is documented at only ~80-90% consistency.
    """
    rules: list[dict] = []
    for p in pronunciations or []:
        word, respell = _get(p, "word"), _get(p, "respelling")
        if word and respell:
            rules.append({"string_to_replace": str(word), "type": "alias",
                          "alias": str(respell)})
    return rules


# ── Curated voice catalog ────────────────────────────────────────────────
# 8 vibes. The LLM picks a vibe; code maps it to a concrete voice_id. All
# voices are curated Indian-accent ElevenLabs shared-library voices. Female
# covers all 8 vibes; male covers 7 — there is no dedicated youthful_excited
# male, so voice_for() falls back to upbeat_lively for that case.
# Swap voice_ids here when a better candidate appears — no prompt changes
# needed since the vibe abstraction stays stable.

VoiceVibe = Literal[
    "upbeat_lively",        # fun/energetic UGC reels — the default
    "warm_relatable",       # BFF/casual/relatable, story/testimonial
    "sassy_expressive",     # sass/humor/gossip-reel/controversial, bold/deep
    "youthful_excited",     # beauty/skincare/fashion punchy
    "professional_clear",   # tech/finance/explainer authority
    "soothing_narrative",   # calm/wellness/storytime
    "crisp_informative",    # crisp, precise how-to / demo / data clarity
    "mature_authentic",     # mature, grounded, middle-aged testimonial
]


@dataclass(frozen=True)
class CatalogEntry:
    voice_id: str
    name: str
    tags: str   # short description shown to the LLM during vibe selection


VOICE_CATALOG: dict[str, dict[VoiceVibe, CatalogEntry]] = {
    "female": {
        "upbeat_lively": CatalogEntry(
            "vA6BjF0oPktVp5b30KzH",
            "Ayesha — Expressive Reel Creator",
            "expressive social-media reel energy — fun, confident, top reels pick",
        ),
        "warm_relatable": CatalogEntry(
            "LJk8s1oqYd18q18FQ9Eg",
            "Ria — Next-door Chatty Girl",
            "casual next-door BFF, chill — storytime / testimonial / relatable",
        ),
        "sassy_expressive": CatalogEntry(
            "Ih3XRGwQe2qczi6DzW48",
            "Aparna K — High-Energy Content Creator",
            "high-energy, excited, expressive — gossip reels / bold hooks",
        ),
        "youthful_excited": CatalogEntry(
            "TmPeb2hSxdVrThJLywkg",
            "Vanishree — Energetic Indian English",
            "energetic crisp youthful punch — beauty / skincare / fashion",
        ),
        "professional_clear": CatalogEntry(
            "zELrJnEGQSGWhiTNcUKq",
            "Nimmo — Clear Hinglish",
            "clear professional Hinglish — explainer / tech / finance / customer-care",
        ),
        "soothing_narrative": CatalogEntry(
            "lHfFvRpJdoR0FStenmrc",
            "Keshavi — Sweet Storyteller",
            "sweet calm storyteller — wellness / storytime / soft narrative",
        ),
        "crisp_informative": CatalogEntry(
            "C3aqZfd4M0h7Ys5tWGIS",
            "Monika Sogam — Crisp & Precise",
            "crisp precise pleasant — how-to / demo / numbers & data clarity",
        ),
        "mature_authentic": CatalogEntry(
            "mmSZflZFDoe6qEecRgIO",
            "India — Husky & Mature",
            "husky mature confident, middle-aged — grounded testimonial / voice-of-experience",
        ),
    },
    "male": {
        "upbeat_lively": CatalogEntry(
            "cQA1lL0fn7iAFW1w50SR",
            "Akash — Confident and Natural",
            "confident natural content-creator energy — social-media reels",
        ),
        "warm_relatable": CatalogEntry(
            "omLr0bN17lYIC1JWLSYV",
            "Bunty — Warm and Clear",
            "warm, relaxed and clear — casual / relatable / entertainment",
        ),
        "sassy_expressive": CatalogEntry(
            "6TcvxMZXgg9AlJrd8iCl",
            "Harshit Arya — Strong, Deep and Casual",
            "strong deep casual, bold — opinion / controversial hooks",
        ),
        # NOTE: no dedicated youthful_excited male in this set — voice_for()
        # falls back to upbeat_lively (Akash), the closest energy.
        "professional_clear": CatalogEntry(
            "wJ5MX7uuKXZwFqGdWM4N",
            "Raj — Professional Indian English",
            "professional, clear, authoritative — explainer / tech / finance",
        ),
        "soothing_narrative": CatalogEntry(
            "1aP69VftkGksPi01itpR",
            "Ram — Slow, Clear, Calm",
            "slow clear calm — soft narrative / wellness / explainer",
        ),
        "crisp_informative": CatalogEntry(
            "TslMG3tSWzKKCzddnWLG",
            "Dhawal — Friendly & Professional",
            "friendly crisp informative — how-to / demo / step-by-step",
        ),
        "mature_authentic": CatalogEntry(
            "gad8DmXGyu7hwftX9JqI",
            "Lohi — Kind, Authentic and Helpful",
            "kind authentic helpful, middle-aged — grounded testimonial",
        ),
    },
}


def voice_for(gender: str, vibe: str) -> str:
    """Resolve (gender, vibe) → voice_id from the catalog.

    Defaults to 'male' if gender is ambiguous, and to 'upbeat_lively' if the
    vibe isn't in the catalog (forward-compat with new vibes).
    """
    g = "female" if (gender or "").lower().strip().startswith("f") else "male"
    bucket = VOICE_CATALOG[g]
    entry = bucket.get(vibe) or bucket["upbeat_lively"]
    return entry.voice_id


def multilingual_anchor_voice_id(gender: str, vibe: str) -> str:
    """Voice_id for the timbre anchor. The catalog is already Indian-English;
    eleven_v3 is multilingual, so the same id carries Hindi. Kept as a named
    seam so the anchor voice can later diverge from the per-vibe pick if a
    specific voice proves more Hindi-robust."""
    return voice_for(gender, vibe)


def catalog_for_prompt(gender: str | None = None) -> str:
    """Render the catalog as a human-readable block for an LLM prompt.

    If `gender` is given, only that gender's voices are shown. Otherwise both
    are listed (the LLM has the character.gender to filter).
    """
    lines: list[str] = []
    for g in (["female", "male"] if gender is None
              else ["female" if gender.lower().startswith("f") else "male"]):
        lines.append(f"### {g.upper()} VOICES")
        for vibe, e in VOICE_CATALOG[g].items():
            lines.append(f"  - {vibe}: {e.name} — {e.tags}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Alignment:
    """Character-level timing from ElevenLabs. All lists are index-aligned."""
    characters: list[str]
    starts: list[float]   # character_start_times_seconds
    ends: list[float]     # character_end_times_seconds

    @property
    def duration(self) -> float:
        return self.ends[-1] if self.ends else 0.0

    def to_dict(self) -> dict:
        return {"characters": self.characters, "starts": self.starts, "ends": self.ends}

    @classmethod
    def from_dict(cls, d: dict) -> "Alignment":
        return cls(characters=d["characters"], starts=d["starts"], ends=d["ends"])


@dataclass(frozen=True)
class VoiceResult:
    audio_path: str
    alignment: Alignment


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attr-or-dict access. SDK returns pydantic objects; tests pass dicts/mocks."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class ElevenLabsVoice:
    def __init__(self, settings: Settings, *, _client: Any = None):
        if not settings.elevenlabs_api_key:
            raise RuntimeError(
                "ELEVENLABS_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if _client is not None:
            self._client = _client
        else:
            from elevenlabs.client import AsyncElevenLabs
            self._client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
        # Pronunciation state: the planner's words for THIS run, and the locator
        # of the dictionary built from them (created lazily, once).
        self._pronunciations: list[Any] = []
        self._pron_locators: list[dict] | None = None

    def set_pronunciations(self, pronunciations: Any) -> None:
        """Register this run's planner-emitted respellings (see gemini.Pronunciation).

        Changing them invalidates any dictionary already built.
        """
        self._pronunciations = list(pronunciations or [])
        self._pron_locators = None

    async def _ensure_pron_locators(self) -> list[dict] | None:
        """Build this run's alias dictionary once; return its locator list.

        One dictionary holds every word because ElevenLabs allows at most 3
        locators per request. A failure here must never kill a run — the read
        just keeps the default pronunciation, so we swallow and disable.
        """
        if self._pron_locators is not None:
            return self._pron_locators or None
        rules = alias_rules(self._pronunciations)
        if not USE_PRONUNCIATION_FIX or not rules:
            self._pron_locators = []
            return None
        try:
            pd = await self._client.pronunciation_dictionaries.create_from_rules(
                name="ugc-run-pronunciations", rules=rules,
            )
            pid = _get(pd, "id")
            ver = _get(pd, "version_id")
            self._pron_locators = ([{"pronunciation_dictionary_id": pid,
                                     "version_id": ver}] if pid else [])
        except Exception:  # noqa: BLE001
            self._pron_locators = []
        return self._pron_locators or None

    async def generate_vo(
        self, *, script: str, voice_id: str, out_path: str | Path,
        model: str = ELEVENLABS_MODEL,
        output_format: str = "mp3_44100_128",
        voice_settings: Any = None,
        seed: int | None = None,
        apply_text_normalization: str = "auto",
        voice_vibe: str | None = None,
    ) -> VoiceResult:
        """Voice the full script; write audio to out_path; return audio + alignment.

        Expressiveness on eleven_v3 does NOT come from the sliders — it comes
        from the SCRIPT (audio tags like [excited]/[laughs], CAPS emphasis, and
        … pauses, all emitted by the Gemini planner). The settings just keep the
        delivery in the expressive band without destabilizing an accented IVC:
          - stability → eleven_v3 stability is a 3-VALUE ENUM, not a
            continuous slider: 0.0 Creative / 0.5 Natural / 1.0 Robust. The
            default is config.ELEVENLABS_STABILITY (0.0 Creative as of
            2026-07-29 — the mode that actually ACTS on audio tags; 0.5
            Natural compounded with the old 1-2-tag cap into monotone).
          - style=0.0 → ElevenLabs docs say keep style at 0 for eleven_v3; >0
            adds latency and mildly destabilizes accented voices. It does NOT
            buy youthful energy (the old comment was wrong).
          - speed=_VO_SPEED (1.15) → v3 accepts speed alongside stability and
            style (verified against SDK 2.50.0 VoiceSettings); default 1.0
            reads ~187 wpm, which is draggy for UGC.
          - similarity_boost / use_speaker_boost are OMITTED — neither is
            supported on eleven_v3 (silent no-ops).

        `voice_vibe` is accepted and IGNORED — it exists so every provider shares
        one call signature. On eleven_v3 expressiveness rides the script's audio
        tags, not a per-generation emotion parameter (that is Cartesia's model).
        """
        # eleven_v3 ignores SSML <break> tags and reads them literally — strip
        # any that slipped into the script; pacing comes from punctuation/tags.
        script = _SSML_BREAK_RE.sub("", script)
        if voice_settings is None:
            try:
                from elevenlabs import VoiceSettings
                voice_settings = VoiceSettings(
                    stability=ELEVENLABS_STABILITY,
                    style=0.0,             # MUST be 0 on v3 (energy comes from the script)
                    speed=_VO_SPEED,       # >1.0 = brisker; default 1.0 read ~187 wpm (draggy)
                )
                # NOTE: similarity_boost and use_speaker_boost are deliberately
                # NOT set — neither is supported on eleven_v3. (stability,
                # style and speed ARE accepted — verified SDK 2.50.0; an older
                # comment claiming v3 takes "stability alone" was wrong.)
            except Exception as exc:  # noqa: BLE001
                # A silent None here would quietly drop the 1.15x UGC speed
                # boost (API default 1.0 ≈ 187 wpm reads slow) — log it so a
                # future SDK signature change is a visible regression.
                print(f"[voice] VoiceSettings unavailable ({exc!r}) — "
                      f"falling back to API defaults (speed boost LOST)")
                voice_settings = None
        kwargs: dict[str, Any] = dict(
            voice_id=voice_id, text=script, model_id=model,
            output_format=output_format,
            # 'auto' = ElevenLabs decides; callers may force 'off'/'on' for
            # number-heavy ad copy. Documented on convert_with_timestamps.
            apply_text_normalization=apply_text_normalization,
        )
        if voice_settings is not None:
            kwargs["voice_settings"] = voice_settings
        # Alias rules for this run's unusual words. Server-side substitution, so
        # the returned alignment keeps the ORIGINAL spelling (verified) and the
        # master clock is unaffected.
        locators = await self._ensure_pron_locators()
        if locators:
            kwargs["pronunciation_dictionary_locators"] = locators
        if seed is not None:
            # Best-effort determinism for re-rollable reads (range 0..4294967295).
            kwargs["seed"] = seed
        # language_code is intentionally NOT set: eleven_v3 auto-detects, and
        # forcing an ISO code the model doesn't support returns a server error
        # (per ElevenLabs docs). The script is English-medium (Hinglish).
        try:
            resp = await self._client.text_to_speech.convert_with_timestamps(**kwargs)
        except TypeError:
            # Older SDK signature without seed/apply_text_normalization — retry
            # with core kwargs only so a version skew never hard-breaks the run.
            for k in ("seed", "apply_text_normalization",
                      "pronunciation_dictionary_locators"):
                kwargs.pop(k, None)
            resp = await self._client.text_to_speech.convert_with_timestamps(**kwargs)

        # SDK field is `audio_base_64` (confirmed: elevenlabs 2.50
        # AudioWithTimestampsResponse).
        audio_b64 = _get(resp, "audio_base_64")
        align = _get(resp, "alignment")
        alignment = Alignment(
            characters=list(_get(align, "characters") or []),
            starts=list(_get(align, "character_start_times_seconds") or []),
            ends=list(_get(align, "character_end_times_seconds") or []),
        )
        if not audio_b64:
            raise RuntimeError("ElevenLabs returned no audio data")
        # The character-level alignment is the master clock for the whole
        # pipeline — beat_windows reads every cut boundary from it. A degenerate
        # or empty alignment would silently collapse every beat to a 0.4s window
        # on a PAID render, so fail fast here instead.
        if (not alignment.characters or not alignment.starts
                or len(alignment.characters) != len(alignment.starts)
                or len(alignment.starts) != len(alignment.ends)):
            raise RuntimeError(
                "ElevenLabs returned an empty or inconsistent alignment "
                f"(chars={len(alignment.characters)}, "
                f"starts={len(alignment.starts)}, ends={len(alignment.ends)}); "
                "cannot derive beat windows."
            )

        # ElevenLabs bills the characters SUBMITTED, so meter the string we
        # actually sent (post <break> strip, post respellings) rather than the
        # caller's original script.
        costs.record_tts(provider="elevenlabs", model=model,
                         characters=len(kwargs["text"]))

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(audio_b64))
        return VoiceResult(audio_path=str(out), alignment=alignment)

    async def transcribe(self, *, media_path) -> str:
        """ElevenLabs Scribe STT on a media file's audio track.

        Used by the native-timbre QA gate to verify each generated clip said
        exactly the scripted line. Runs the blocking SDK call in a thread.
        The temp WAV is always removed (try/finally) so it does not accumulate
        when the pipeline processes many clips.
        """
        media_path = Path(media_path)
        wav = media_path.with_suffix(".stt.wav")
        try:
            await asyncio.to_thread(_extract_audio, media_path, wav)

            def _call():
                with open(wav, "rb") as fh:
                    return self._client.speech_to_text.convert(
                        model_id="scribe_v1", file=fh)

            resp = await asyncio.to_thread(_call)
            # Scribe bills per hour of audio; the WAV we just extracted is
            # exactly what was submitted, so its duration is the billed one.
            costs.record_stt(model="scribe_v1",
                             seconds=_audio_duration(wav))
            return (getattr(resp, "text", "") or "").strip()
        finally:
            wav.unlink(missing_ok=True)

    async def force_align(self, *, media_path: str | Path, text: str) -> Alignment:
        """Character-level timing for KNOWN text against arbitrary audio
        (ElevenLabs Forced Alignment).

        Native r2v RE-RENDERS speech (~±0.3s of internal re-timing), so the
        TTS alignment no longer describes the audio in the final video —
        this re-anchors the script against the audio that actually plays,
        giving caption timing for native mode. The temp WAV is always
        removed (try/finally).
        """
        media_path = Path(media_path)
        wav = media_path.with_suffix(".align.wav")
        try:
            await asyncio.to_thread(_extract_audio, media_path, wav)
            with open(wav, "rb") as fh:
                resp = await self._client.forced_alignment.create(
                    file=fh, text=text)
            chars = _get(resp, "characters") or []
            alignment = Alignment(
                characters=[_get(c, "text") or "" for c in chars],
                starts=[float(_get(c, "start") or 0.0) for c in chars],
                ends=[float(_get(c, "end") or 0.0) for c in chars],
            )
            if not alignment.characters:
                raise RuntimeError(
                    "ElevenLabs forced alignment returned no characters"
                )
            # ElevenLabs publishes no rate for the Forced Alignment endpoint,
            # so this is flagged rather than priced at a guessed number. It is
            # a per-run rounding error next to the clips either way.
            costs.note_unpriced("elevenlabs:forced_alignment")
            return alignment
        finally:
            wav.unlink(missing_ok=True)

    async def list_indian_voices(self) -> list[dict]:
        """List premade Indian-accent voices (filter by labels.accent)."""
        resp = await self._client.voices.search(
            search="indian", category="premade", page_size=100,
        )
        voices = _get(resp, "voices") or []
        out: list[dict] = []
        for v in voices:
            labels = _get(v, "labels") or {}
            accent = labels.get("accent") if isinstance(labels, dict) else None
            out.append({
                "voice_id": _get(v, "voice_id"),
                "name": _get(v, "name"),
                "accent": accent,
            })
        return out


# ── TTS-only providers ───────────────────────────────────────────────────────
# Neither returns a char-level alignment usable as the pipeline's master clock,
# so each implements a narrow contract — prep_text() + synthesize() — and
# AlignedVoice below supplies the clock. Keeping them this small is deliberate:
# all the timing risk stays in ONE place.


async def _write_audio(resp: Any, out: Path) -> None:
    """Persist whatever shape a TTS SDK handed back.

    SDK responses vary (raw bytes, a binary-response wrapper with
    write_to_file/content/read, or an async byte iterator) and tests inject
    plain bytes. Normalising here keeps the provider classes readable.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(resp, (bytes, bytearray)):
        out.write_bytes(bytes(resp))
        return
    writer = getattr(resp, "write_to_file", None)
    if writer is not None:
        result = writer(str(out))
        if asyncio.iscoroutine(result):
            await result
        return
    content = getattr(resp, "content", None)
    if isinstance(content, (bytes, bytearray)):
        out.write_bytes(bytes(content))
        return
    reader = getattr(resp, "read", None)
    if reader is not None:
        data = reader()
        if asyncio.iscoroutine(data):
            data = await data
        out.write_bytes(bytes(data))
        return
    if hasattr(resp, "__aiter__"):
        buf = bytearray()
        async for chunk in resp:
            buf.extend(chunk)
        out.write_bytes(bytes(buf))
        return
    raise RuntimeError(f"cannot persist TTS response of type {type(resp).__name__}")


class CartesiaVoice:
    """Cartesia sonic-3.5 TTS (audio only).

    Uses POST /tts/bytes (via the SDK's tts.generate) to get an mp3 in one call.
    The alternative, /tts/sse, is the ONLY Cartesia endpoint that returns
    timestamps — but it also forces container="raw", i.e. headerless PCM chunks
    that would need assembling and transcoding. Since AlignedVoice recovers the
    clock anyway, taking the mp3 path is both simpler and cheaper.
    """

    provider = "cartesia"

    def __init__(self, settings: Settings, *, _client: Any = None):
        if not settings.cartesia_api_key:
            raise RuntimeError(
                "CARTESIA_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if _client is not None:
            self._client = _client
        else:
            from cartesia import AsyncCartesia
            self._client = AsyncCartesia(
                api_key=settings.cartesia_api_key,
                # The SDK pins a stale version header; override so errors arrive
                # as structured JSON instead of plain text.
                default_headers={"Cartesia-Version": CARTESIA_API_VERSION},
                max_retries=3,
            )
        self._pronunciations: list[Any] = []

    def set_pronunciations(self, pronunciations: Any) -> None:
        self._pronunciations = list(pronunciations or [])

    def prep_text(self, script: str) -> str:
        text = prep_for_cartesia(script)
        if USE_PRONUNCIATION_FIX:
            # Applied to the SENT text only. AlignedVoice force-aligns against
            # spoken_text(script), which keeps the original spelling, so the
            # master clock and captions are unaffected.
            text = apply_respellings(text, getattr(self, "_pronunciations", []))
        return text

    async def synthesize(self, *, script: str, voice_id: str, out_path: Path,
                         voice_vibe: str | None = None) -> str:
        """Speak `script` in `voice_id`; write mp3 to out_path; return the text
        actually sent (the caller force-aligns against exactly this)."""
        transcript = self.prep_text(script)
        if not transcript:
            raise RuntimeError("Cartesia: nothing to speak after text prep")
        generation_config: dict[str, Any] = {
            # A FLOAT in [0.6, 1.5] on sonic-3/3.5. The top-level `speed` enum
            # (slow/normal/fast) is deprecated and unreliable per-voice.
            "speed": CARTESIA_SPEED,
            "volume": 1.0,
            # Guidance, not a strict adjustment — and weaker on instant clones,
            # which are never tagged "Emotive". Speed + punctuation do more work.
            "emotion": cartesia_emotion_for_vibe(voice_vibe),
        }
        resp = await self._client.tts.generate(
            model_id=CARTESIA_MODEL,
            transcript=transcript,
            voice={"mode": "id", "id": voice_id},   # "embedding" mode removed 2026-06-01
            language="en",   # scripts are English-medium Hinglish
            # mp3 takes bit_rate, NOT encoding — mixing them up is an easy 422.
            output_format={"container": "mp3", "sample_rate": 44100,
                           "bit_rate": 192000},
            generation_config=generation_config,
        )
        await _write_audio(resp, Path(out_path))
        costs.record_tts(provider="cartesia", model=CARTESIA_MODEL,
                         characters=len(transcript))
        return transcript


class FishAudioVoice:
    """Fish Audio s2.1-pro TTS (audio only), over raw httpx.

    Deliberately NOT using fish-audio-sdk 1.3.0: its Model literal still names
    the dead speech-1.5/1.6 and cannot name s2.1-pro, its opus_bitrate default
    (32) is not a legal API value, and its defaults silently differ from the
    API's. The REST call is a bearer header plus one JSON body, so direct httpx
    is both safer and smaller.
    """

    provider = "fish"

    def __init__(self, settings: Settings, *, _client: Any = None):
        if not settings.fish_api_key:
            raise RuntimeError(
                "FISH_API_KEY is not set. Add it to .env (see .env.example)."
            )
        self._api_key = settings.fish_api_key
        self._client = _client
        self._pronunciations: list[Any] = []

    def set_pronunciations(self, pronunciations: Any) -> None:
        self._pronunciations = list(pronunciations or [])

    def prep_text(self, script: str) -> str:
        text = prep_for_fish(script)
        if USE_PRONUNCIATION_FIX:
            # Applied to the SENT text only. AlignedVoice force-aligns against
            # spoken_text(script), which keeps the original spelling, so the
            # master clock and captions are unaffected.
            text = apply_respellings(text, getattr(self, "_pronunciations", []))
        return text

    async def _post(self, path: str, *, json_body: dict, headers: dict) -> Any:
        if self._client is not None:
            return await self._client.post(path, json=json_body, headers=headers)
        import httpx
        async with httpx.AsyncClient(base_url=FISH_API_BASE, timeout=240.0) as c:
            return await c.post(path, json=json_body, headers=headers)

    async def synthesize(self, *, script: str, voice_id: str, out_path: Path,
                         voice_vibe: str | None = None) -> str:
        """Speak `script` in `voice_id`; write mp3 to out_path; return the text
        actually sent. `voice_vibe` is unused — Fish takes its expressiveness
        from the [bracket] tags prep_for_fish leaves inline."""
        text = self.prep_text(script)
        if not text:
            raise RuntimeError("Fish Audio: nothing to speak after text prep")
        body = {
            "text": text,
            "reference_id": voice_id,
            "format": "mp3",
            "mp3_bitrate": 192,
            "sample_rate": 44100,
            "latency": "normal",
            "temperature": 0.7,
            "top_p": 0.7,
            "chunk_length": 300,
            "normalize": True,           # expands numbers/dates
            # Keeps timbre consistent across internally-chunked long text.
            "condition_on_previous_chunks": True,
            # Post-hoc time-stretch, range 0.5-2.0. Fish exposes no `seed`.
            "prosody": {"speed": FISH_SPEED, "volume": 0},
        }
        resp = await self._post(
            "/v1/tts", json_body=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                # Required header. JSON is fine here — msgpack is only needed
                # for inline `references`, and we clone up front instead.
                "model": FISH_MODEL,
            },
        )
        status = getattr(resp, "status_code", 200)
        if status == 402:
            raise RuntimeError("Fish Audio: out of credit (HTTP 402)")
        if status >= 400:
            detail = ""
            try:
                detail = (getattr(resp, "text", "") or "")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"Fish Audio TTS failed (HTTP {status}): {detail}")
        audio = getattr(resp, "content", None)
        if not audio:
            raise RuntimeError("Fish Audio returned no audio data")
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(bytes(audio))
        costs.record_tts(provider="fish", model=FISH_MODEL,
                         characters=len(text))
        return text


class AlignedVoice:
    """Gives a TTS-only provider the full voice-client contract.

    The pipeline's master clock is a CHAR-LEVEL alignment (assembly.beat_windows
    reads every cut boundary from it). Only ElevenLabs returns one alongside its
    audio, so for other providers we synthesize first and then recover the clock
    with ElevenLabs Forced Alignment against the exact text that was spoken.

    Forced alignment — rather than a provider's own word timestamps — is the
    deliberate choice: it measures the audio that ACTUALLY plays, and it gives
    every provider one identically-shaped clock with no per-provider timing code.
    The cost is one extra ElevenLabs call per TTS, and ELEVENLABS_API_KEY
    remaining required regardless of the selected provider.
    """

    def __init__(self, provider: Any, aligner: ElevenLabsVoice):
        self._provider = provider
        self._aligner = aligner
        self.provider = getattr(provider, "provider", "unknown")

    def set_pronunciations(self, pronunciations: Any) -> None:
        """Forward to the provider, which applies the respellings to the text it
        SENDS. The aligner deliberately gets none — force_align is handed the
        original spelling so the clock stays canonical."""
        setter = getattr(self._provider, "set_pronunciations", None)
        if setter is not None:
            setter(pronunciations)

    async def generate_vo(
        self, *, script: str, voice_id: str, out_path: str | Path,
        voice_vibe: str | None = None, **_ignored: Any,
    ) -> VoiceResult:
        """Same contract as ElevenLabsVoice.generate_vo.

        ElevenLabs-specific kwargs (model, voice_settings, seed,
        apply_text_normalization, output_format) are accepted and ignored so
        callers stay provider-agnostic.
        """
        out = Path(out_path)
        await self._provider.synthesize(
            script=script, voice_id=voice_id, out_path=out, voice_vibe=voice_vibe,
        )
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(
                f"{self.provider} wrote no audio to {out}"
            )
        # CRITICAL: align against the plain SPOKEN WORDS — tags removed, casing
        # UNTOUCHED — not against the provider-specific text we sent.
        #
        # assembly.beat_windows finds each beat via
        #   align_text.find(_strip_tags(beat.dialogue_text))
        # and assembly._strip_tags removes [brackets] but PRESERVES CAPS. So the
        # alignment's characters must read exactly like the planner's script
        # minus its tags — which is also precisely what ElevenLabs' own
        # alignment returns (its API consumes the tags and echoes the rest
        # verbatim). Aligning against the provider text instead would inject
        # Fish's "[emphasis] " (extra characters) and lowercase Cartesia's
        # emphasis words, so "It is INSANE" would never be found in
        # "It is [emphasis] insane" — find() fails, the cursor fallback takes
        # over, and every subsequent cut drifts against the audio.
        align_text = spoken_text(script)
        if not align_text.strip():
            raise RuntimeError(
                f"{self.provider}: script has no speakable words to align"
            )
        alignment = await self._aligner.force_align(media_path=out, text=align_text)
        # Same fail-fast as the ElevenLabs path: a degenerate alignment would
        # silently collapse every beat to a 0.4s window on a PAID render.
        if (not alignment.characters or not alignment.starts
                or len(alignment.characters) != len(alignment.starts)
                or len(alignment.starts) != len(alignment.ends)):
            raise RuntimeError(
                f"forced alignment for {self.provider} audio returned an empty "
                f"or inconsistent alignment (chars={len(alignment.characters)}, "
                f"starts={len(alignment.starts)}, ends={len(alignment.ends)}); "
                "cannot derive beat windows."
            )
        return VoiceResult(audio_path=str(out), alignment=alignment)

    async def transcribe(self, *, media_path) -> str:
        """QC fidelity gate — always ElevenLabs Scribe."""
        return await self._aligner.transcribe(media_path=media_path)

    async def force_align(self, *, media_path: str | Path, text: str) -> Alignment:
        return await self._aligner.force_align(media_path=media_path, text=text)


def make_voice_client(settings: Settings, provider: str | None = None) -> Any:
    """Build the voice client for `provider` ("elevenlabs"/"cartesia"/"fish").

    Every returned client satisfies the same contract — generate_vo /
    transcribe / force_align — so the pipeline needs no provider branches.
    Unknown provider names fall back to ElevenLabs rather than failing a run.
    """
    name = (provider or VOICE_PROVIDER or "elevenlabs").strip().lower()
    if name == "cartesia":
        return AlignedVoice(CartesiaVoice(settings), ElevenLabsVoice(settings))
    if name in ("fish", "fish_audio", "fishaudio"):
        return AlignedVoice(FishAudioVoice(settings), ElevenLabsVoice(settings))
    return ElevenLabsVoice(settings)
