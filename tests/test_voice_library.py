import json

import pytest

from app.config import Settings
from app.gemini import CharacterDescription, GeminiClient, VoicePersona
from app.voice_library import (
    VoiceProfile,
    candidates_for_gender,
    load_voice_library,
    provider_id_field,
)


def _settings():
    return Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="gem-test",
        elevenlabs_api_key="el", sync_api_key="sy", llm_provider="gemini",
    )


def _mock_genai_client(text_value: str):
    from unittest.mock import AsyncMock, MagicMock
    fake = MagicMock()
    fake.aio.models.generate_content = AsyncMock(
        return_value=MagicMock(text=text_value))
    return fake


def _char(**ov) -> CharacterDescription:
    fields = dict(
        age=26, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="long_loose_natural_waves",
        setting="apartment_kitchen_counter", expression="calm_mid_sentence",
        hair="long dark waves", face="warm eyes", wardrobe="cream tank",
        energy="warm kitchen energy", voice_vibe="warm_relatable",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm",
                                   warmth="friendly"),
    )
    fields.update(ov)
    return CharacterDescription(**fields)


def _rec(vid, gender, selected=True, **ch):
    base_ch = dict(gender=gender, language="English", age_band="early-20s",
                   timbre="bright clear", pitch="medium", pace="upbeat",
                   tone_warmth="friendly", energy="high", voice_vibe="upbeat_lively",
                   summary="a young voice")
    base_ch.update(ch)
    return {"key": f"insta/{vid}", "selected": selected,
            "elevenlabs_voice_id": vid, "elevenlabs_name": f"OCLabs-{vid}",
            "s3_url": "https://x/y.mp3", "source_file": f"{vid}.mp3",
            "characteristics": base_ch}


def test_load_filters_unselected_and_missing_id(tmp_path):
    data = [
        _rec("v1", "female"),
        _rec("v2", "male", selected=False),          # not selected → skip
        {"key": "k3", "selected": True, "elevenlabs_voice_id": None,
         "characteristics": {}},                      # no id → skip
        _rec("v4", "male"),
    ]
    p = tmp_path / "voices.json"
    p.write_text(json.dumps(data))
    profs = load_voice_library(p)
    assert {p.voice_id for p in profs} == {"v1", "v4"}
    assert all(isinstance(p, VoiceProfile) for p in profs)


def test_load_missing_file_returns_empty(tmp_path):
    assert load_voice_library(tmp_path / "nope.json") == []


# ── provider scoping ─────────────────────────────────────────────────────────
# The same record carries one id column per provider (the `key` is the voice
# identity). Loading for a provider must return ONLY voices that provider can
# actually speak — the matcher is constrained rather than falling back silently
# to another engine's id.

def test_provider_id_field_maps_providers():
    assert provider_id_field("elevenlabs") == "elevenlabs_voice_id"
    assert provider_id_field("cartesia") == "cartesia_voice_id"
    assert provider_id_field("fish") == "fish_voice_id"
    # aliases + unknown fall back to the default provider's column
    assert provider_id_field("fish_audio") == "fish_voice_id"
    assert provider_id_field("nope") == "elevenlabs_voice_id"


def test_load_is_scoped_to_the_selected_provider(tmp_path):
    a = _rec("v1", "female")
    a["cartesia_voice_id"] = "car-1"
    a["fish_voice_id"] = "fish-1"
    b = _rec("v2", "male")          # ElevenLabs only — never cloned elsewhere
    p = tmp_path / "voices.json"
    p.write_text(json.dumps([a, b]))

    assert {x.voice_id for x in load_voice_library(p, provider="elevenlabs")} \
        == {"v1", "v2"}
    # v2 has no Cartesia/Fish clone, so it must not be offered there.
    assert {x.voice_id for x in load_voice_library(p, provider="cartesia")} \
        == {"car-1"}
    assert {x.voice_id for x in load_voice_library(p, provider="fish")} \
        == {"fish-1"}


def test_load_returns_empty_when_provider_has_no_clones(tmp_path):
    p = tmp_path / "voices.json"
    p.write_text(json.dumps([_rec("v1", "female")]))
    assert load_voice_library(p, provider="cartesia") == []


def test_provider_scoped_profile_keeps_the_same_identity(tmp_path):
    """Switching providers changes the engine, not who is speaking."""
    rec = _rec("v1", "female", voice_vibe="warm_relatable")
    rec["cartesia_voice_id"] = "car-1"
    p = tmp_path / "voices.json"
    p.write_text(json.dumps([rec]))

    el = load_voice_library(p, provider="elevenlabs")[0]
    car = load_voice_library(p, provider="cartesia")[0]
    assert el.voice_id != car.voice_id          # different engine id
    assert (el.gender, el.voice_vibe, el.summary) == \
           (car.gender, car.voice_vibe, car.summary)   # same person


def test_gender_norm_and_candidates(tmp_path):
    data = [_rec("f1", "F"), _rec("m1", "M"),
            _rec("a1", "ambiguous")]
    p = tmp_path / "v.json"; p.write_text(json.dumps(data))
    profs = load_voice_library(p)
    fem = {c.voice_id for c in candidates_for_gender(profs, "female")}
    male = {c.voice_id for c in candidates_for_gender(profs, "male")}
    # ambiguous voices are eligible for either gender
    assert fem == {"f1", "a1"}
    assert male == {"m1", "a1"}


@pytest.mark.asyncio
async def test_match_voice_returns_chosen_id():
    fake = _mock_genai_client(json.dumps(
        {"voice_id": "v1", "reason": "age + energy fit"}))
    c = GeminiClient(_settings(), _client=fake)
    profs = [VoiceProfile("v1", "n1", "female", "English", "early-20s",
                          "bright", "medium", "upbeat", "friendly", "high",
                          "upbeat_lively", "young bright voice")]
    m = await c.match_voice(character=_char(), profiles=profs)
    assert m.voice_id == "v1"


@pytest.mark.asyncio
async def test_match_voice_nulls_hallucinated_id():
    # Model returns an id that isn't among the candidates → treated as no-fit.
    fake = _mock_genai_client(json.dumps(
        {"voice_id": "GHOST", "reason": "made up"}))
    c = GeminiClient(_settings(), _client=fake)
    profs = [VoiceProfile("v1", "n1", "female", "English", "early-20s",
                          "bright", "medium", "upbeat", "friendly", "high",
                          "upbeat_lively", "young")]
    m = await c.match_voice(character=_char(), profiles=profs)
    assert m.voice_id is None


@pytest.mark.asyncio
async def test_match_voice_empty_profiles_short_circuits():
    # No genai call needed when there are no candidates.
    c = GeminiClient(_settings(), _client=_mock_genai_client("{}"))
    m = await c.match_voice(character=_char(), profiles=[])
    assert m.voice_id is None
