import base64
import types

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.voice import Alignment, ElevenLabsVoice, script_fidelity


def _settings(elevenlabs_key="el-xxx"):
    return Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="g",
        elevenlabs_api_key=elevenlabs_key, sync_api_key="sy",
    )


def _fake_client(audio_bytes=b"fake-mp3-bytes"):
    align = MagicMock(
        characters=["H", "i", " ", "y", "o"],
        character_start_times_seconds=[0.0, 0.1, 0.2, 0.3, 0.4],
        character_end_times_seconds=[0.1, 0.2, 0.3, 0.4, 0.5],
    )
    resp = MagicMock(
        audio_base_64=base64.b64encode(audio_bytes).decode(),
        alignment=align,
    )
    fake = MagicMock()
    fake.text_to_speech.convert_with_timestamps = AsyncMock(return_value=resp)
    return fake


def test_requires_api_key():
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        ElevenLabsVoice(_settings(elevenlabs_key=""))


@pytest.mark.asyncio
async def test_generate_vo_writes_audio_and_parses_alignment(tmp_path):
    fake = _fake_client(b"hello-mp3")
    v = ElevenLabsVoice(_settings(), _client=fake)
    out = tmp_path / "vo.mp3"

    result = await v.generate_vo(script="Hi yo", voice_id="vid-1", out_path=out)

    # Audio decoded + written.
    assert out.read_bytes() == b"hello-mp3"
    assert result.audio_path == str(out)
    # Alignment parsed.
    assert result.alignment.characters == ["H", "i", " ", "y", "o"]
    assert result.alignment.starts[0] == 0.0
    assert result.alignment.duration == 0.5
    # Correct args sent to SDK.
    kw = fake.text_to_speech.convert_with_timestamps.call_args.kwargs
    assert kw["voice_id"] == "vid-1"
    assert kw["text"] == "Hi yo"
    assert kw["model_id"] == "eleven_v3"


@pytest.mark.asyncio
async def test_generate_vo_strips_ssml_break_tags(tmp_path):
    """eleven_v3 reads <break/> literally — they must be stripped before send."""
    fake = _fake_client()
    v = ElevenLabsVoice(_settings(), _client=fake)
    await v.generate_vo(
        script='Wait… <break time="0.8s"/> it ACTUALLY worked',
        voice_id="v", out_path=tmp_path / "a.mp3",
    )
    sent = fake.text_to_speech.convert_with_timestamps.call_args.kwargs["text"]
    assert "<break" not in sent
    # Prosody punctuation (… and CAPS) is preserved.
    assert "…" in sent and "ACTUALLY" in sent


@pytest.mark.asyncio
async def test_generate_vo_default_settings_are_v3_safe(tmp_path):
    """Only the parameters eleven_v3 actually honours may be sent.

    stability is the GAIN on the script's audio tags: 0.0 Creative / 0.5 Natural
    / 1.0 Robust ("less responsive to directional prompts"). We ship 0.0 because
    the read measured FLAT — the planner now directs delivery with ~1 tag per
    sentence, and Creative is the mode that acts on them.

    similarity_boost and use_speaker_boost are NOT supported on v3 (the v3-only
    text-to-dialogue settings object accepts `stability` alone), so sending them
    was a silent no-op dressed up as an accent safeguard. They must stay absent.
    """
    fake = _fake_client()
    v = ElevenLabsVoice(_settings(), _client=fake)
    await v.generate_vo(script="hi", voice_id="v", out_path=tmp_path / "a.mp3")
    vs = fake.text_to_speech.convert_with_timestamps.call_args.kwargs.get("voice_settings")
    if vs is not None:   # None only if the elevenlabs SDK isn't importable
        assert vs.stability == 0.0
        assert vs.style == 0.0
        assert getattr(vs, "similarity_boost", None) is None
        assert getattr(vs, "use_speaker_boost", None) in (None, False)


@pytest.mark.asyncio
async def test_generate_vo_raises_on_empty_alignment(tmp_path):
    """Audio present but alignment empty must fail fast — a degenerate alignment
    would silently collapse every beat to a 0.4s window on a paid render."""
    align = MagicMock(
        characters=[], character_start_times_seconds=[],
        character_end_times_seconds=[],
    )
    resp = MagicMock(audio_base_64=base64.b64encode(b"audio").decode(), alignment=align)
    fake = MagicMock()
    fake.text_to_speech.convert_with_timestamps = AsyncMock(return_value=resp)
    v = ElevenLabsVoice(_settings(), _client=fake)

    with pytest.raises(RuntimeError, match="alignment"):
        await v.generate_vo(script="x", voice_id="v", out_path=tmp_path / "a.mp3")


@pytest.mark.asyncio
async def test_generate_vo_raises_on_inconsistent_alignment(tmp_path):
    """Mismatched alignment array lengths are also caught (chars != starts)."""
    align = MagicMock(
        characters=["a", "b", "c"],
        character_start_times_seconds=[0.0, 0.1],   # one short
        character_end_times_seconds=[0.1, 0.2],
    )
    resp = MagicMock(audio_base_64=base64.b64encode(b"audio").decode(), alignment=align)
    fake = MagicMock()
    fake.text_to_speech.convert_with_timestamps = AsyncMock(return_value=resp)
    v = ElevenLabsVoice(_settings(), _client=fake)

    with pytest.raises(RuntimeError, match="alignment"):
        await v.generate_vo(script="x", voice_id="v", out_path=tmp_path / "a.mp3")


@pytest.mark.asyncio
async def test_generate_vo_forwards_seed_and_normalization(tmp_path):
    fake = _fake_client()
    v = ElevenLabsVoice(_settings(), _client=fake)
    await v.generate_vo(
        script="hi", voice_id="v", out_path=tmp_path / "a.mp3",
        seed=4242, apply_text_normalization="off",
    )
    kw = fake.text_to_speech.convert_with_timestamps.call_args.kwargs
    assert kw["seed"] == 4242
    assert kw["apply_text_normalization"] == "off"


@pytest.mark.asyncio
async def test_generate_vo_omits_seed_when_none(tmp_path):
    fake = _fake_client()
    v = ElevenLabsVoice(_settings(), _client=fake)
    await v.generate_vo(script="hi", voice_id="v", out_path=tmp_path / "a.mp3")
    kw = fake.text_to_speech.convert_with_timestamps.call_args.kwargs
    assert "seed" not in kw                       # not forwarded when unset
    assert kw["apply_text_normalization"] == "auto"   # default still sent


@pytest.mark.asyncio
async def test_generate_vo_raises_on_empty_audio(tmp_path):
    align = MagicMock(
        characters=[], character_start_times_seconds=[],
        character_end_times_seconds=[],
    )
    resp = MagicMock(audio_base_64="", alignment=align)
    fake = MagicMock()
    fake.text_to_speech.convert_with_timestamps = AsyncMock(return_value=resp)
    v = ElevenLabsVoice(_settings(), _client=fake)

    with pytest.raises(RuntimeError, match="no audio data"):
        await v.generate_vo(script="x", voice_id="v", out_path=tmp_path / "a.mp3")


def test_voice_for_resolves_per_gender_and_vibe():
    from app.voice import VOICE_CATALOG, voice_for

    # Female + upbeat_lively → Ayesha
    assert voice_for("female", "upbeat_lively") == "vA6BjF0oPktVp5b30KzH"
    # Male + upbeat_lively → Akash
    assert voice_for("male", "upbeat_lively") == "cQA1lL0fn7iAFW1w50SR"
    # All 8 vibes present for female; male covers 7 (no youthful_excited male).
    vibes = {"upbeat_lively", "warm_relatable", "sassy_expressive",
             "youthful_excited", "professional_clear", "soothing_narrative",
             "crisp_informative", "mature_authentic"}
    assert set(VOICE_CATALOG["female"].keys()) == vibes
    assert set(VOICE_CATALOG["male"].keys()) == vibes - {"youthful_excited"}


def test_voice_for_falls_back_on_unknown_vibe():
    from app.voice import voice_for, VOICE_CATALOG
    # Unknown vibe → defaults to upbeat_lively for that gender.
    assert voice_for("female", "made_up_vibe") == \
        VOICE_CATALOG["female"]["upbeat_lively"].voice_id


def test_voice_for_male_youthful_excited_falls_back_to_upbeat():
    """No dedicated youthful_excited male voice → falls back to upbeat_lively."""
    from app.voice import voice_for, VOICE_CATALOG
    assert voice_for("male", "youthful_excited") == \
        VOICE_CATALOG["male"]["upbeat_lively"].voice_id


def test_catalog_voice_ids_are_unique_within_gender():
    """The new catalog uses a distinct voice per slot (no duplicate IDs like
    the old male catalog, which reused Bunty across three vibes)."""
    from app.voice import VOICE_CATALOG
    for gender, bucket in VOICE_CATALOG.items():
        ids = [e.voice_id for e in bucket.values()]
        assert len(ids) == len(set(ids)), f"duplicate voice_id in {gender}"


def test_anchor_voice_is_a_catalog_voice():
    from app.voice import multilingual_anchor_voice_id
    vid = multilingual_anchor_voice_id("female", "warm_relatable")
    assert isinstance(vid, str) and len(vid) > 5


def test_catalog_for_prompt_renders():
    from app.voice import catalog_for_prompt
    block = catalog_for_prompt()
    assert "FEMALE VOICES" in block and "MALE VOICES" in block
    assert "upbeat_lively" in block and "Ayesha" in block and "Bunty" in block
    # New vibes are rendered too.
    assert "crisp_informative" in block and "mature_authentic" in block
    # Single-gender rendering (use header-prefix to avoid the "MALE" ⊂ "FEMALE" substring trap)
    only_f = catalog_for_prompt("female")
    assert "### FEMALE VOICES" in only_f and "### MALE VOICES" not in only_f


def test_alignment_roundtrip():
    a = Alignment(characters=["a", "b"], starts=[0.0, 0.1], ends=[0.1, 0.2])
    assert Alignment.from_dict(a.to_dict()) == a
    assert a.duration == 0.2


# ── script_fidelity tests ────────────────────────────────────────────────────

def test_script_fidelity_ignores_paralinguistics_and_scores_overlap():
    expected = "Wait, you have to hear about my morning routine."
    # Exact words + Seedance-added paralinguistics → should score ~1.0
    transcript = "(sighs) Wait, you have to hear about my morning routine. (birds chirping)"
    assert script_fidelity(transcript, expected) >= 0.95


def test_script_fidelity_flags_offscript_drift():
    expected = "Wait, you have to hear about my morning routine."
    transcript = "Buy now and subscribe to my channel for more deals today."
    assert script_fidelity(transcript, expected) < 0.3


def test_script_fidelity_empty_expected_is_one():
    # silent beats carry empty dialogue → treat as trivially faithful
    assert script_fidelity("(birds chirping)", "") == 1.0


def test_script_fidelity_partial_overlap_lands_between_thresholds():
    # expected: 6 unique words — {this, product, changed, my, morning, routine}
    # transcript shares 3 words (my, morning, routine) and adds 3 new ones
    # (tried, a, different) → union = 9, intersection = 3 → Jaccard = 3/9 ≈ 0.333
    expected = "This product changed my morning routine."
    transcript = "I tried a different my morning routine."
    score = script_fidelity(transcript, expected)
    # Must be strictly between the drift threshold (0.2) and match threshold (0.8)
    assert 0.2 < score < 0.8, f"expected partial overlap score, got {score}"


# ── transcribe tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcribe_extracts_audio_and_calls_scribe(tmp_path, monkeypatch):
    # Fake ffmpeg extraction so no real media is needed.
    monkeypatch.setattr("app.voice._extract_audio",
                        lambda src, dst: dst.write_bytes(b"wav"))
    fake = MagicMock()
    fake.speech_to_text.convert.return_value = types.SimpleNamespace(
        text="hello world")
    v = ElevenLabsVoice(_settings(), _client=fake)

    text = await v.transcribe(media_path=tmp_path / "clip.mp4")

    assert text == "hello world"
    kw = fake.speech_to_text.convert.call_args.kwargs
    assert kw["model_id"] == "scribe_v1"


@pytest.mark.asyncio
async def test_force_align_maps_characters(tmp_path, monkeypatch):
    """Forced alignment re-anchors KNOWN text against arbitrary audio and
    returns the same Alignment shape the caption pipeline consumes."""
    import app.voice as V

    chars = [MagicMock(text=c, start=i * 0.2, end=(i + 1) * 0.2)
             for i, c in enumerate("hi yo")]
    fake = MagicMock()
    fake.forced_alignment.create = AsyncMock(
        return_value=MagicMock(characters=chars))
    monkeypatch.setattr(V, "_extract_audio",
                        lambda src, dst: dst.write_bytes(b"wav"))
    v = ElevenLabsVoice(_settings(), _client=fake)
    media = tmp_path / "final.mp4"
    media.write_bytes(b"mp4")

    al = await v.force_align(media_path=media, text="hi yo")

    assert al.characters == list("hi yo")
    assert al.starts[0] == 0.0 and al.ends[-1] == pytest.approx(1.0)
    # The known script text is what gets aligned.
    assert fake.forced_alignment.create.call_args.kwargs["text"] == "hi yo"
    # Temp wav cleaned up.
    assert not (tmp_path / "final.align.wav").exists()


@pytest.mark.asyncio
async def test_force_align_empty_response_raises(tmp_path, monkeypatch):
    import app.voice as V

    fake = MagicMock()
    fake.forced_alignment.create = AsyncMock(
        return_value=MagicMock(characters=[]))
    monkeypatch.setattr(V, "_extract_audio",
                        lambda src, dst: dst.write_bytes(b"wav"))
    v = ElevenLabsVoice(_settings(), _client=fake)
    media = tmp_path / "final.mp4"
    media.write_bytes(b"mp4")

    with pytest.raises(RuntimeError, match="no characters"):
        await v.force_align(media_path=media, text="hi")
