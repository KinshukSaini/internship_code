"""Multi-provider voice: per-provider text prep, AlignedVoice, provider routing.

The text-prep tests encode API facts verified against the live Cartesia and Fish
docs (2026-07-29) — most importantly that Cartesia reads ALL-CAPS letter by
letter and has no inline tag vocabulary, while Fish s2.1-pro uses the same
[bracket] syntax family as eleven_v3 and so can keep the planner's
expressiveness. A regression here degrades audio SILENTLY, which is exactly why
these are unit-tested rather than left to a live listen.
"""
from pathlib import Path

import pytest

from app.config import Settings
from app.voice import (
    AlignedVoice,
    Alignment,
    CartesiaVoice,
    ElevenLabsVoice,
    FishAudioVoice,
    cartesia_emotion_for_vibe,
    decaps_emphasis,
    make_voice_client,
    prep_for_cartesia,
    prep_for_fish,
    spoken_text,
    strip_audio_tags,
)


def _settings(**ov):
    base = dict(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="g",
        elevenlabs_api_key="el", sync_api_key="sy",
        cartesia_api_key="sk_car_x", fish_api_key="fish-x",
    )
    base.update(ov)
    return Settings(**base)


# ── text prep ────────────────────────────────────────────────────────────────

def test_strip_audio_tags_removes_eleven_v3_tags():
    assert strip_audio_tags("[excited] Hey [laughs] there") == "Hey there"


def test_decaps_lowercases_emphasis_words():
    # Cartesia reads all-caps letter by letter, so INSANE must not survive.
    assert decaps_emphasis("this is INSANE") == "this is insane"


def test_decaps_spares_short_acronyms():
    # UV/SPF/AC are 2-3 chars: letter-by-letter is the CORRECT reading, so the
    # 4-char floor must leave them alone.
    assert decaps_emphasis("SPF 50 and UV rays") == "SPF 50 and UV rays"


def test_prep_for_cartesia_strips_tags_and_caps_but_keeps_pacing():
    out = prep_for_cartesia("[excited] It is INSANE… really — promise")
    assert "[" not in out
    assert "INSANE" not in out and "insane" in out
    # … and — are Cartesia's primary pacing levers; they must survive.
    assert "…" in out and "—" in out


def test_prep_for_fish_maps_tags_to_fish_vocabulary():
    out = prep_for_fish("[laughs] okay [sighs] fine [giggles] yes")
    assert "[laughing]" in out
    assert "[sighing]" in out
    assert "[chuckling]" in out
    # The eleven_v3 spellings must NOT leak through — Fish would speak them.
    assert "[laughs]" not in out and "[sighs]" not in out


def test_prep_for_fish_converts_caps_to_emphasis_marker():
    assert prep_for_fish("this is INSANE") == "this is [emphasis] insane"


def test_prep_for_fish_drops_unknown_tags():
    # An unmapped tag would be read aloud as text — dropping beats guessing.
    out = prep_for_fish("[whimsical shrug] hello")
    assert "whimsical" not in out
    assert out == "hello"


def test_prep_for_fish_keeps_known_tag_unchanged():
    assert prep_for_fish("[excited] go") == "[excited] go"


@pytest.mark.parametrize("vibe,expected", [
    ("upbeat_lively", "excited"),
    ("soothing_narrative", "calm"),
    ("professional_clear", "neutral"),
    ("", "neutral"),
    ("not_a_vibe", "neutral"),
])
def test_cartesia_emotion_for_vibe(vibe, expected):
    assert cartesia_emotion_for_vibe(vibe) == expected


# ── provider clients ─────────────────────────────────────────────────────────

class _FakeCartesiaSDK:
    def __init__(self):
        self.calls = []
        self.tts = self
        self.audio = b"cartesia-mp3"

    async def generate(self, **kw):
        self.calls.append(kw)
        return self.audio


class _FakeFishResponse:
    def __init__(self, content=b"fish-mp3", status_code=200, text=""):
        self.content = content
        self.status_code = status_code
        self.text = text


class _FakeHTTPX:
    def __init__(self, resp=None):
        self.resp = resp or _FakeFishResponse()
        self.calls = []

    async def post(self, path, *, json=None, headers=None):
        self.calls.append({"path": path, "json": json, "headers": headers})
        return self.resp


@pytest.mark.asyncio
async def test_cartesia_synthesize_writes_mp3_and_sends_prepped_text(tmp_path):
    sdk = _FakeCartesiaSDK()
    v = CartesiaVoice(_settings(), _client=sdk)
    out = tmp_path / "vo.mp3"
    spoken = await v.synthesize(
        script="[excited] It is INSANE", voice_id="cv-1", out_path=out,
        voice_vibe="upbeat_lively")

    assert out.read_bytes() == b"cartesia-mp3"
    # The returned text is what gets force-aligned, so it must equal what was sent.
    assert spoken == sdk.calls[0]["transcript"] == "It is insane"
    kw = sdk.calls[0]
    assert kw["voice"] == {"mode": "id", "id": "cv-1"}
    # mp3 takes bit_rate, never encoding.
    assert kw["output_format"]["container"] == "mp3"
    assert "bit_rate" in kw["output_format"]
    assert "encoding" not in kw["output_format"]
    assert kw["generation_config"]["emotion"] == "excited"
    assert isinstance(kw["generation_config"]["speed"], float)


@pytest.mark.asyncio
async def test_fish_synthesize_writes_mp3_and_keeps_tags(tmp_path):
    http = _FakeHTTPX()
    v = FishAudioVoice(_settings(), _client=http)
    out = tmp_path / "vo.mp3"
    spoken = await v.synthesize(
        script="[laughs] so GOOD", voice_id="fish-1", out_path=out)

    assert out.read_bytes() == b"fish-mp3"
    body = http.calls[0]["json"]
    assert spoken == body["text"] == "[laughing] so [emphasis] good"
    assert body["reference_id"] == "fish-1"
    assert body["format"] == "mp3"
    assert http.calls[0]["path"] == "/v1/tts"
    # The `model` header is required by the API.
    assert http.calls[0]["headers"]["model"]
    assert http.calls[0]["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_fish_out_of_credit_is_a_clear_error(tmp_path):
    http = _FakeHTTPX(_FakeFishResponse(content=b"", status_code=402))
    v = FishAudioVoice(_settings(), _client=http)
    with pytest.raises(RuntimeError, match="out of credit"):
        await v.synthesize(script="hi", voice_id="f", out_path=tmp_path / "a.mp3")


def test_provider_clients_require_their_key():
    with pytest.raises(RuntimeError, match="CARTESIA_API_KEY"):
        CartesiaVoice(_settings(cartesia_api_key=""))
    with pytest.raises(RuntimeError, match="FISH_API_KEY"):
        FishAudioVoice(_settings(fish_api_key=""))


# ── AlignedVoice ─────────────────────────────────────────────────────────────

class _FakeProvider:
    provider = "cartesia"

    def __init__(self, *, text="hi there", write=True):
        self.text = text
        self.write = write
        self.calls = []

    def prep_text(self, script):
        return self.text

    async def synthesize(self, *, script, voice_id, out_path, voice_vibe=None):
        self.calls.append({"script": script, "voice_id": voice_id,
                           "voice_vibe": voice_vibe})
        if self.write:
            Path(out_path).write_bytes(b"audio")
        return self.text


class _FakeAligner:
    def __init__(self, alignment=None):
        self.alignment = alignment or Alignment(
            characters=list("hi"), starts=[0.0, 0.1], ends=[0.1, 0.2])
        self.align_calls = []
        self.transcribe_calls = []

    async def force_align(self, *, media_path, text):
        self.align_calls.append({"media_path": str(media_path), "text": text})
        return self.alignment

    async def transcribe(self, *, media_path):
        self.transcribe_calls.append(str(media_path))
        return "transcribed"


@pytest.mark.asyncio
async def test_aligned_voice_recovers_clock_from_spoken_text(tmp_path):
    prov, aligner = _FakeProvider(text="hi"), _FakeAligner()
    v = AlignedVoice(prov, aligner)
    out = tmp_path / "vo.mp3"

    res = await v.generate_vo(script="[excited] hi", voice_id="c1",
                              out_path=out, voice_vibe="upbeat_lively")

    assert res.audio_path == str(out)
    assert res.alignment is aligner.alignment
    # Aligned against the tag-stripped SCRIPT, not the provider-specific text.
    assert aligner.align_calls[0]["text"] == " hi"
    assert prov.calls[0]["voice_vibe"] == "upbeat_lively"


@pytest.mark.asyncio
async def test_alignment_text_is_tag_free_but_case_preserving(tmp_path):
    """The master-clock invariant, and a real bug this caught.

    assembly.beat_windows locates a beat with
    align_text.find(_strip_tags(beat.dialogue_text)) — and assembly._strip_tags
    drops [brackets] while PRESERVING CAPS. So the alignment text must equal the
    script minus its tags, with casing untouched.

    Aligning against what the provider was sent would break this: Fish's prep
    inserts "[emphasis] " and lowercases the word, so "It is INSANE" would not
    be found in "It is [emphasis] insane" — find() fails, the cursor fallback
    silently takes over, and every later cut drifts against the audio.
    """
    script = "[excited] It is INSANE… so good"
    # A provider whose prep mangles the text the way Fish's really does.
    prov = _FakeProvider(text="[excited] It is [emphasis] insane… so good")
    aligner = _FakeAligner()
    await AlignedVoice(prov, aligner).generate_vo(
        script=script, voice_id="c1", out_path=tmp_path / "a.mp3")

    aligned = aligner.align_calls[0]["text"]
    # Byte-faithful: the space the tag sat on is preserved, exactly as
    # assembly._strip_tags would leave it.
    assert aligned == " It is INSANE… so good"
    assert "[" not in aligned                 # tag-free
    assert "INSANE" in aligned                # casing preserved
    assert "[emphasis]" not in aligned        # provider markup excluded

    # The invariant itself: a beat's tag-stripped dialogue must be findable.
    from app.assembly import _strip_tags
    assert aligned.find(_strip_tags("[excited] It is INSANE")) == 0
    assert aligned == _strip_tags(script)


@pytest.mark.asyncio
async def test_aligned_voice_rejects_a_script_with_no_words(tmp_path):
    v = AlignedVoice(_FakeProvider(), _FakeAligner())
    with pytest.raises(RuntimeError, match="no speakable words"):
        await v.generate_vo(script="[sighs]", voice_id="c1",
                            out_path=tmp_path / "a.mp3")


@pytest.mark.asyncio
async def test_aligned_voice_ignores_elevenlabs_only_kwargs(tmp_path):
    v = AlignedVoice(_FakeProvider(), _FakeAligner())
    res = await v.generate_vo(
        script="hi", voice_id="c1", out_path=tmp_path / "a.mp3",
        model="eleven_v3", seed=7, apply_text_normalization="off",
        voice_settings=object(), output_format="mp3_44100_128")
    assert res.alignment.duration > 0


@pytest.mark.asyncio
async def test_aligned_voice_rejects_degenerate_alignment(tmp_path):
    # A collapsed clock would silently squash every beat to a 0.4s window on a
    # PAID render, so this must fail loudly.
    bad = Alignment(characters=[], starts=[], ends=[])
    v = AlignedVoice(_FakeProvider(), _FakeAligner(bad))
    with pytest.raises(RuntimeError, match="inconsistent alignment"):
        await v.generate_vo(script="hi", voice_id="c1",
                            out_path=tmp_path / "a.mp3")


@pytest.mark.asyncio
async def test_aligned_voice_fails_when_provider_wrote_nothing(tmp_path):
    v = AlignedVoice(_FakeProvider(write=False), _FakeAligner())
    with pytest.raises(RuntimeError, match="no audio"):
        await v.generate_vo(script="hi", voice_id="c1",
                            out_path=tmp_path / "a.mp3")


@pytest.mark.asyncio
async def test_aligned_voice_delegates_transcribe_and_force_align(tmp_path):
    aligner = _FakeAligner()
    v = AlignedVoice(_FakeProvider(), aligner)
    assert await v.transcribe(media_path=tmp_path / "v.mp4") == "transcribed"
    await v.force_align(media_path=tmp_path / "v.mp4", text="hi")
    assert aligner.transcribe_calls and aligner.align_calls


# ── routing ──────────────────────────────────────────────────────────────────

def test_make_voice_client_routes_by_provider():
    s = _settings()
    assert isinstance(make_voice_client(s, "elevenlabs"), ElevenLabsVoice)
    for name in ("cartesia", "fish", "fish_audio"):
        client = make_voice_client(s, name)
        assert isinstance(client, AlignedVoice)
    assert make_voice_client(s, "cartesia").provider == "cartesia"
    assert make_voice_client(s, "fish").provider == "fish"


def test_make_voice_client_unknown_provider_falls_back_to_elevenlabs():
    assert isinstance(make_voice_client(_settings(), "nope"), ElevenLabsVoice)


def test_spoken_text_matches_assembly_strip_tags():
    """The alignment text and the beat-matching text MUST agree byte for byte.

    beat_windows does align_text.find(_strip_tags(dialogue)); if these two
    strippers ever diverge the find() misses and every cut drifts, silently.
    """
    from app.assembly import _strip_tags
    for s in [
        "[excited] It is INSANE… so good",
        "no tags at all",
        "[laughs]",
        "trailing tag [sighs]",
        "back[a]to[b]back",
        "  leading and trailing  ",
        "multi\nline [excited] text",
        "",
    ]:
        assert spoken_text(s) == _strip_tags(s), s


# ── Pronunciation repair ─────────────────────────────────────────────────────
# The planner emits {word, respelling} per RUN (gemini.Pronunciation) — there is
# deliberately NO static table of terms, because every brief brings new brand
# names and the planner already knows how the words it wrote should sound.

class _Pron:
    def __init__(self, word, respelling):
        self.word, self.respelling = word, respelling


def test_apply_respellings_is_whole_word_and_case_insensitive():
    from app.voice import apply_respellings
    prons = [_Pron("Ayurvedic", "aa-yur-VAY-dik")]
    assert apply_respellings("This Ayurvedic oil", prons) == "This aa-yur-VAY-dik oil"
    # sentence-initial / lowercase both match
    assert apply_respellings("ayurvedic oil", prons) == "aa-yur-VAY-dik oil"
    # but a substring inside another word must NOT be replaced
    assert apply_respellings("Ayurvedically", prons) == "Ayurvedically"


def test_apply_respellings_accepts_dicts_and_pairs():
    from app.voice import apply_respellings
    assert apply_respellings("say Ayurveda", [{"word": "Ayurveda",
                                               "respelling": "aa-yur-VAY-da"}]) \
        == "say aa-yur-VAY-da"
    assert apply_respellings("say X", [("X", "eks")]) == "say eks"


def test_apply_respellings_noop_without_pronunciations():
    from app.voice import apply_respellings
    assert apply_respellings("untouched", []) == "untouched"
    assert apply_respellings("untouched", None) == "untouched"


def test_alias_rules_shape():
    """ElevenLabs alias rules — NOT phoneme rules. Alias works on every model,
    is a deterministic pre-inference substitution, and needs no IPA (inline
    <phoneme> SSML does not work on eleven_v3 at all)."""
    from app.voice import alias_rules
    rules = alias_rules([_Pron("Ayurvedic", "aa-yur-VAY-dik"), _Pron("", "x")])
    assert rules == [{"string_to_replace": "Ayurvedic", "type": "alias",
                      "alias": "aa-yur-VAY-dik"}]


@pytest.mark.asyncio
async def test_fish_applies_respellings_to_sent_text_only(tmp_path):
    """The respelling goes into the text the PROVIDER receives; the master clock
    is force-aligned against the original spelling, so captions and beat
    matching keep reading 'Ayurvedic'."""
    http = _FakeHTTPX()
    v = FishAudioVoice(_settings(), _client=http)
    v.set_pronunciations([_Pron("Ayurvedic", "aa-yur-VAY-dik")])
    aligner = _FakeAligner()
    await AlignedVoice(v, aligner).generate_vo(
        script="This Ayurvedic oil works.", voice_id="f",
        out_path=tmp_path / "a.mp3")

    assert "aa-yur-VAY-dik" in http.calls[0]["json"]["text"]     # sent respelled
    assert aligner.align_calls[0]["text"] == "This Ayurvedic oil works."  # clock canonical


@pytest.mark.asyncio
async def test_cartesia_applies_respellings_to_sent_text(tmp_path):
    sdk = _FakeCartesiaSDK()
    v = CartesiaVoice(_settings(), _client=sdk)
    v.set_pronunciations([_Pron("Ayurvedic", "aa-yur-VAY-dik")])
    await v.synthesize(script="This Ayurvedic oil.", voice_id="c",
                       out_path=tmp_path / "a.mp3")
    assert "aa-yur-VAY-dik" in sdk.calls[0]["transcript"]


@pytest.mark.asyncio
async def test_elevenlabs_never_substitutes_locally(tmp_path):
    """CRITICAL: on ElevenLabs the substitution must happen SERVER-side via an
    alias dictionary, never in our text. Verified 2026-07-29 that the returned
    alignment then keeps the original spelling; a local substitution would put
    the respelling into alignment.characters and break beat_windows' find()."""
    import base64
    from unittest.mock import AsyncMock, MagicMock
    align = MagicMock(characters=list("hi"),
                      character_start_times_seconds=[0.0, 0.1],
                      character_end_times_seconds=[0.1, 0.2])
    resp = MagicMock(audio_base_64=base64.b64encode(b"a").decode(), alignment=align)
    fake = MagicMock()
    fake.text_to_speech.convert_with_timestamps = AsyncMock(return_value=resp)
    fake.pronunciation_dictionaries.create_from_rules = AsyncMock(
        return_value=MagicMock(id="pd1", version_id="v1"))

    v = ElevenLabsVoice(_settings(), _client=fake)
    v.set_pronunciations([_Pron("Ayurvedic", "aa-yur-VAY-dik")])
    await v.generate_vo(script="This Ayurvedic oil.", voice_id="x",
                        out_path=tmp_path / "a.mp3")

    kw = fake.text_to_speech.convert_with_timestamps.call_args.kwargs
    assert kw["text"] == "This Ayurvedic oil."          # NOT respelled locally
    assert kw["pronunciation_dictionary_locators"] == [
        {"pronunciation_dictionary_id": "pd1", "version_id": "v1"}]


@pytest.mark.asyncio
async def test_elevenlabs_dictionary_failure_never_kills_the_run(tmp_path):
    import base64
    from unittest.mock import AsyncMock, MagicMock
    align = MagicMock(characters=list("hi"),
                      character_start_times_seconds=[0.0, 0.1],
                      character_end_times_seconds=[0.1, 0.2])
    resp = MagicMock(audio_base_64=base64.b64encode(b"a").decode(), alignment=align)
    fake = MagicMock()
    fake.text_to_speech.convert_with_timestamps = AsyncMock(return_value=resp)
    fake.pronunciation_dictionaries.create_from_rules = AsyncMock(
        side_effect=RuntimeError("quota"))
    v = ElevenLabsVoice(_settings(), _client=fake)
    v.set_pronunciations([_Pron("Ayurvedic", "aa-yur-VAY-dik")])
    # Must still produce audio — just with default pronunciation.
    res = await v.generate_vo(script="hi", voice_id="x", out_path=tmp_path / "a.mp3")
    assert res.alignment.duration > 0
    assert "pronunciation_dictionary_locators" not in \
        fake.text_to_speech.convert_with_timestamps.call_args.kwargs
