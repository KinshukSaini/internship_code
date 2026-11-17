"""ClaudeBackend + GeminiClient claude-provider routing.

Pins the per-tier request shapes (model IDs, thinking/effort/temperature
rules), the schema vs text return paths, refusal handling, and the
fail-fast key checks — all against a fake anthropic client, no network.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from app.claude import ClaudeBackend
from app.config import (
    CLAUDE_FAST_MODEL,
    CLAUDE_LITE_MODEL,
    CLAUDE_PRO_MODEL,
    Settings,
)
from app.gemini import GeminiClient, VoicePersona


def _settings(**over):
    base = dict(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="",
        elevenlabs_api_key="el", sync_api_key="sy",
        anthropic_api_key="ant-test", llm_provider="claude",
    )
    base.update(over)
    return Settings(**base)


class _Toy(BaseModel):
    x: int


def _parse_resp(parsed, *, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason,
                           stop_details=stop_details)


def _text_resp(text, *, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=stop_details,
    )


class _FakeStreamCM:
    """Async context manager standing in for messages.stream(...)."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return SimpleNamespace(
            get_final_message=AsyncMock(return_value=self._resp))

    async def __aexit__(self, *exc):
        return False


def _fake_anthropic(*, parse_resp=None, create_resp=None):
    # Both paths now go through ONE streaming seam — messages.stream — so
    # parse_resp/create_resp both feed the same fake stream.
    fake = MagicMock()
    resp = parse_resp if parse_resp is not None else create_resp
    fake.messages.stream = MagicMock(return_value=_FakeStreamCM(resp))
    return fake


@pytest.mark.asyncio
async def test_generate_attaches_image_url_as_multimodal_content():
    fake = _fake_anthropic(create_resp=_text_resp("a prompt"))
    backend = ClaudeBackend(_settings(), _client=fake)
    out = await backend.generate(
        tier="fast", prompt="describe the motion",
        image_url="https://host/anchor-3.png",
    )
    assert out == "a prompt"
    msgs = fake.messages.stream.call_args.kwargs["messages"]
    assert msgs == [{
        "role": "user",
        "content": [
            {"type": "image",
             "source": {"type": "url", "url": "https://host/anchor-3.png"}},
            {"type": "text", "text": "describe the motion"},
        ],
    }]


@pytest.mark.asyncio
async def test_generate_without_image_url_keeps_string_content():
    fake = _fake_anthropic(create_resp=_text_resp("x"))
    backend = ClaudeBackend(_settings(), _client=fake)
    await backend.generate(tier="fast", prompt="hello")
    msgs = fake.messages.stream.call_args.kwargs["messages"]
    assert msgs == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_generate_image_url_survives_the_pro_tier():
    """The multimodal user turn is built independent of tier — Opus (pro) must
    still receive the image + text content, with its adaptive-thinking / no-temp
    request shape intact."""
    fake = _fake_anthropic(parse_resp=_parse_resp(_Toy(x=7)))
    backend = ClaudeBackend(_settings(), _client=fake)
    out = await backend.generate(
        tier="pro", prompt="motion please", schema=_Toy, temperature=0.9,
        image_url="https://host/anchor-0.png",
    )
    assert isinstance(out, _Toy) and out.x == 7
    kw = fake.messages.stream.call_args.kwargs
    assert kw["model"] == CLAUDE_PRO_MODEL
    assert kw["thinking"] == {"type": "adaptive"}
    assert "temperature" not in kw          # opus still drops the temp
    assert kw["messages"] == [{
        "role": "user",
        "content": [
            {"type": "image",
             "source": {"type": "url", "url": "https://host/anchor-0.png"}},
            {"type": "text", "text": "motion please"},
        ],
    }]


@pytest.mark.asyncio
async def test_generate_empty_image_url_degrades_to_text_only():
    """An empty-string image_url carries no image and degrades to a plain
    text user turn — the guard is truthy (`if image_url:`), so `""` never
    builds an image block with a blank url (which the Anthropic API rejects)."""
    fake = _fake_anthropic(create_resp=_text_resp("y"))
    backend = ClaudeBackend(_settings(), _client=fake)
    await backend.generate(tier="fast", prompt="hi", image_url="")
    msgs = fake.messages.stream.call_args.kwargs["messages"]
    assert msgs == [{"role": "user", "content": "hi"}]


def test_backend_requires_anthropic_api_key():
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ClaudeBackend(_settings(anthropic_api_key=""))


@pytest.mark.asyncio
async def test_pro_tier_opus_adaptive_thinking_no_temperature():
    fake = _fake_anthropic(parse_resp=_parse_resp(_Toy(x=1)))
    backend = ClaudeBackend(_settings(), _client=fake)
    out = await backend.generate(
        tier="pro", prompt="p", schema=_Toy, temperature=0.7,
    )
    assert isinstance(out, _Toy) and out.x == 1
    kw = fake.messages.stream.call_args.kwargs
    assert kw["model"] == CLAUDE_PRO_MODEL
    assert kw["max_tokens"] == 32000
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high"}
    # temperature is REMOVED on opus — sending it 400s.
    assert "temperature" not in kw
    assert kw["output_format"] is _Toy
    assert kw["messages"] == [{"role": "user", "content": "p"}]


@pytest.mark.asyncio
async def test_fast_tier_sonnet_temperature_and_effort_medium():
    fake = _fake_anthropic(parse_resp=_parse_resp(_Toy(x=2)))
    backend = ClaudeBackend(_settings(), _client=fake)
    await backend.generate(tier="fast", prompt="p", schema=_Toy,
                           temperature=0.6)
    kw = fake.messages.stream.call_args.kwargs
    assert kw["model"] == CLAUDE_FAST_MODEL
    assert kw["max_tokens"] == 8192
    assert kw["temperature"] == pytest.approx(0.6)
    assert kw["output_config"] == {"effort": "medium"}
    assert "thinking" not in kw


@pytest.mark.asyncio
async def test_lite_tier_haiku_temperature_no_output_config():
    fake = _fake_anthropic(parse_resp=_parse_resp(_Toy(x=3)))
    backend = ClaudeBackend(_settings(), _client=fake)
    await backend.generate(tier="lite", prompt="p", schema=_Toy,
                           temperature=0.1)
    kw = fake.messages.stream.call_args.kwargs
    assert kw["model"] == CLAUDE_LITE_MODEL
    assert kw["max_tokens"] == 1024
    assert kw["temperature"] == pytest.approx(0.1)
    assert "output_config" not in kw
    assert "thinking" not in kw


@pytest.mark.asyncio
async def test_unknown_tier_raises():
    backend = ClaudeBackend(_settings(), _client=_fake_anthropic())
    with pytest.raises(ValueError, match="tier"):
        await backend.generate(tier="mega", prompt="p")


@pytest.mark.asyncio
async def test_text_path_returns_str_and_caches_system_block():
    fake = _fake_anthropic(create_resp=_text_resp("a seedance prompt"))
    backend = ClaudeBackend(_settings(), _client=fake)
    out = await backend.generate(
        tier="pro", prompt="p", schema=None,
        system_instruction="SKILL MD CONTENT",
    )
    assert out == "a seedance prompt"
    assert isinstance(out, str)
    kw = fake.messages.stream.call_args.kwargs
    assert "output_format" not in kw          # text path sends no schema
    assert kw["system"] == [{
        "type": "text", "text": "SKILL MD CONTENT",
        "cache_control": {"type": "ephemeral"},
    }]


@pytest.mark.asyncio
async def test_list_system_instruction_caches_each_block_as_a_prefix():
    """A list system_instruction becomes one cache_control block per element,
    in order — so a shared skill-MD prefix (block 0) is byte-identical across
    methods and gets its own cache entry, with per-call context after it."""
    fake = _fake_anthropic(create_resp=_text_resp("ok"))
    backend = ClaudeBackend(_settings(), _client=fake)
    await backend.generate(
        tier="pro", prompt="p", schema=None,
        system_instruction=["SKILL MD PREFIX", "PER-CALL TAIL"],
    )
    kw = fake.messages.stream.call_args.kwargs
    assert kw["system"] == [
        {"type": "text", "text": "SKILL MD PREFIX",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "PER-CALL TAIL",
         "cache_control": {"type": "ephemeral"}},
    ]


@pytest.mark.asyncio
async def test_schema_path_omits_system_when_no_instruction():
    fake = _fake_anthropic(parse_resp=_parse_resp(_Toy(x=4)))
    backend = ClaudeBackend(_settings(), _client=fake)
    await backend.generate(tier="fast", prompt="p", schema=_Toy)
    assert "system" not in fake.messages.stream.call_args.kwargs


@pytest.mark.asyncio
async def test_refusal_raises_on_schema_path():
    fake = _fake_anthropic(parse_resp=_parse_resp(
        None, stop_reason="refusal",
        stop_details=SimpleNamespace(category=None, explanation="nope"),
    ))
    backend = ClaudeBackend(_settings(), _client=fake)
    with pytest.raises(ValueError, match="refused"):
        await backend.generate(tier="fast", prompt="p", schema=_Toy)


@pytest.mark.asyncio
async def test_refusal_raises_on_text_path():
    fake = _fake_anthropic(create_resp=_text_resp("", stop_reason="refusal"))
    backend = ClaudeBackend(_settings(), _client=fake)
    with pytest.raises(ValueError, match="refused"):
        await backend.generate(tier="pro", prompt="p")


# ---------- GeminiClient routing on the claude provider ----------

def test_gemini_client_claude_provider_requires_anthropic_key():
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        GeminiClient(_settings(anthropic_api_key=""))


def test_gemini_client_claude_provider_skips_gemini_key():
    # gemini_api_key is "" in _settings() — must NOT raise on the claude path.
    fake = _fake_anthropic()
    c = GeminiClient(_settings(), _claude_client=fake)
    assert c._provider == "claude"


def test_gemini_client_rejects_unknown_provider():
    with pytest.raises(ValueError, match="llm_provider"):
        GeminiClient(_settings(llm_provider="bard"))


@pytest.mark.asyncio
async def test_should_open_with_product_uses_lite_tier():
    from app.gemini import ProductInFirstFrameDecision
    decision = ProductInFirstFrameDecision(
        in_first_frame=True, reasoning="product-forward demo",
    )
    fake = _fake_anthropic(parse_resp=_parse_resp(decision))
    c = GeminiClient(_settings(), _claude_client=fake)
    out = await c.should_open_with_product(
        brief="product demo for X", opening_chunk_action="holds X up",
    )
    assert out is True
    kw = fake.messages.stream.call_args.kwargs
    assert kw["model"] == CLAUDE_LITE_MODEL
    assert kw["output_format"] is ProductInFirstFrameDecision
    assert kw["temperature"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_plan_timeline_routes_to_opus_and_validates(tmp_path):
    from app.gemini import CharacterDescription, TimelineBeat, VideoTimeline
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="long_loose_natural_waves",
        setting="apartment_kitchen_counter", expression="candid_mid_laugh",
        hair="wavy", face="smooth", wardrobe="tee", energy="warm",
        voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )
    tl = VideoTimeline(
        full_script="Okay so I tried this. I love this so much",
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay so I tried this. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="cta",
                         dialogue_text="I love this so much",
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    fake = _fake_anthropic(parse_resp=_parse_resp(tl))
    c = GeminiClient(_settings(), _claude_client=fake)
    res = await c.plan_timeline(brief="a serum", character=char,
                                product_present=True, target_duration_s=20)
    assert res is tl
    kw = fake.messages.stream.call_args.kwargs
    assert kw["model"] == CLAUDE_PRO_MODEL
    assert kw["output_format"] is VideoTimeline
    assert "temperature" not in kw          # opus drops the 0.7
    assert "a serum" in kw["messages"][0]["content"]


@pytest.mark.asyncio
async def test_write_seedance_prompt_routes_to_opus_with_skill_system(tmp_path):
    from app.gemini import CharacterDescription, ChunkSpec
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("DUMMY SKILL CONTENT")
    fake = _fake_anthropic(create_resp=_text_resp("  the prompt  "))
    c = GeminiClient(_settings(), _claude_client=fake,
                     skill_md_path=skill_path)
    chunk = ChunkSpec(index=0, t_start=0, t_end=15, dialogue="hi",
                      action="raises bowl", summary="intro", framing="selfie")
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="long_loose_natural_waves",
        setting="apartment_kitchen_counter", expression="candid_mid_laugh",
        hair="wavy", face="smooth", wardrobe="tee", energy="warm",
        voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )
    out = await c.write_seedance_prompt(chunk=chunk, character=char,
                                        prev_summary=None)
    assert out == "the prompt"              # stripped
    kw = fake.messages.stream.call_args.kwargs
    assert kw["model"] == CLAUDE_PRO_MODEL
    assert kw["system"][0]["text"].startswith("DUMMY SKILL CONTENT")
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_pipeline_fails_fast_when_claude_selected_without_key(tmp_path):
    """Selecting claude with no ANTHROPIC_API_KEY must die before any client
    is built or paid call made."""
    from app.pipeline import generate_ugc_video
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await generate_ugc_video(
            brief="b", settings=_settings(anthropic_api_key=""),
            work_dir=tmp_path, out_path=tmp_path / "o.mp4",
        )
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.asyncio
async def test_pipeline_rejects_unknown_llm_provider(tmp_path):
    from app.pipeline import generate_ugc_video
    with pytest.raises(ValueError, match="llm_provider"):
        await generate_ugc_video(
            brief="b", settings=_settings(), work_dir=tmp_path,
            out_path=tmp_path / "o.mp4", llm_provider="bard",
        )
