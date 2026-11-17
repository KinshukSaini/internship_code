"""Cloned-voice library: the real influencer voices cloned into ElevenLabs.

`scripts/clone_voices.py` + `dedup_select.py` + `clone_selected.py` produce
`data/voice_clones/voices.json`; the `selected` records that own an
`elevenlabs_voice_id` are the pipeline-usable voices. This module loads them as
`VoiceProfile`s so the pipeline can MATCH a voice to a generated character
(character-first: the brief drives the character, the voice is chosen to fit).

Kept deliberately thin — no network, no SDK. Just parse the JSON the clone
scripts already wrote.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import VOICE_LIBRARY_PATH, VOICE_PROVIDER

# Which voices.json field holds each provider's id for the SAME underlying
# voice. The record `key` is the voice identity; provider ids are columns, so
# switching providers keeps the same real influencer and only changes the engine.
PROVIDER_ID_FIELD = {
    "elevenlabs": "elevenlabs_voice_id",
    "cartesia": "cartesia_voice_id",
    "fish": "fish_voice_id",
}


def provider_id_field(provider: str | None) -> str:
    """voices.json id column for `provider`. Unknown → ElevenLabs (the default
    provider, and the only one guaranteed to be populated)."""
    name = (provider or VOICE_PROVIDER or "elevenlabs").strip().lower()
    if name in ("fish_audio", "fishaudio"):
        name = "fish"
    return PROVIDER_ID_FIELD.get(name, "elevenlabs_voice_id")


@dataclass(frozen=True)
class VoiceProfile:
    voice_id: str
    name: str
    gender: str          # 'male' / 'female' / 'ambiguous' (as heard)
    language: str        # 'English', 'Hinglish', ...
    age_band: str        # 'early-20s', 'mid-30s', ...
    timbre: str
    pitch: str
    pace: str
    tone_warmth: str
    energy: str
    voice_vibe: str      # best-fit catalog vibe (VoiceVibe value)
    summary: str
    s3_url: str = ""
    source_file: str = ""

    def prompt_line(self) -> str:
        """Compact one-liner shown to the matcher LLM."""
        return (
            f"[{self.voice_id}] {self.gender}, {self.age_band}, {self.language}; "
            f"timbre={self.timbre}; pitch={self.pitch}; pace={self.pace}; "
            f"tone={self.tone_warmth}; energy={self.energy}; vibe={self.voice_vibe}. "
            f"{self.summary}"
        )


def _gender_norm(g: str | None) -> str:
    g = (g or "").lower().strip()
    if g.startswith("f"):
        return "female"
    if g.startswith("m"):
        return "male"
    return "ambiguous"


def load_voice_library(path: str | Path | None = None,
                       provider: str | None = None) -> list[VoiceProfile]:
    """Load selected, cloned voices from voices.json. Missing file → [].

    Only records that actually carry an id for `provider` are returned, so the
    matcher LLM is CONSTRAINED to voices that exist on the selected engine — no
    silent cross-provider fallback. A provider with no clones yet yields [], and
    the caller falls back to the legacy 8-vibe catalog.
    """
    p = Path(path or VOICE_LIBRARY_PATH)
    if not p.exists():
        return []
    id_field = provider_id_field(provider)
    records = json.loads(p.read_text(encoding="utf-8") or "[]")
    out: list[VoiceProfile] = []
    for r in records:
        if not r.get("selected") or not r.get(id_field):
            continue
        ch = r.get("characteristics") or {}
        out.append(VoiceProfile(
            voice_id=r[id_field],
            name=r.get("elevenlabs_name") or r.get("key") or r[id_field],
            gender=_gender_norm(ch.get("gender")),
            language=ch.get("language") or "",
            age_band=ch.get("age_band") or "",
            timbre=ch.get("timbre") or "",
            pitch=ch.get("pitch") or "",
            pace=ch.get("pace") or "",
            tone_warmth=ch.get("tone_warmth") or "",
            energy=ch.get("energy") or "",
            voice_vibe=ch.get("voice_vibe") or "",
            summary=ch.get("summary") or "",
            s3_url=r.get("s3_url") or "",
            source_file=r.get("source_file") or "",
        ))
    return out


def candidates_for_gender(profiles: list[VoiceProfile],
                          gender: str) -> list[VoiceProfile]:
    """Same-gender candidates (hard constraint — a voice's gender must match
    the character's). Ambiguous-gender voices are eligible for either."""
    g = _gender_norm(gender)
    return [p for p in profiles if p.gender == g or p.gender == "ambiguous"]
