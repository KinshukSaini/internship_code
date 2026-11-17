import pytest
from app.config import Settings, load_settings
import app.config as config_module


@pytest.fixture(autouse=True)
def isolate_dotenv(monkeypatch):
    """Prevent the real .env from leaking into config tests."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **kw: None)


def test_load_settings_reads_all_fields(monkeypatch):
    monkeypatch.setenv("EVOLINK_API_KEY", "evo-abc")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "akid")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")

    s = load_settings()

    assert isinstance(s, Settings)
    assert s.evolink_api_key == "evo-abc"
    assert s.aws_access_key_id == "akid"
    assert s.aws_secret_access_key == "secret"
    assert s.aws_bucket_name == "my-bucket"
    assert s.aws_region == "ap-south-1"


def test_load_settings_raises_when_evolink_key_missing(monkeypatch):
    monkeypatch.delenv("EVOLINK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EVOLINK_API_KEY"):
        load_settings()


def test_load_settings_aws_fields_optional_until_used(monkeypatch):
    monkeypatch.setenv("EVOLINK_API_KEY", "evo-abc")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    s = load_settings()
    assert s.evolink_api_key == "evo-abc"
    assert s.aws_access_key_id == ""
    assert s.aws_secret_access_key == ""


def test_load_settings_reads_gemini_key(monkeypatch):
    monkeypatch.setenv("EVOLINK_API_KEY", "evo-abc")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-xyz")
    s = load_settings()
    assert s.gemini_api_key == "gem-xyz"


def test_load_settings_reads_elevenlabs_and_sync_keys(monkeypatch):
    monkeypatch.setenv("EVOLINK_API_KEY", "evo-abc")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-abc")
    monkeypatch.setenv("SYNC_API_KEY", "sy-abc")
    s = load_settings()
    assert s.elevenlabs_api_key == "el-abc"
    assert s.sync_api_key == "sy-abc"


def test_voice_lipsync_model_constants():
    from app.config import (
        DEFAULT_LIPSYNC_ENGINE, ELEVENLABS_MODEL, NATIVE_LIPSYNC_MODEL,
        SYNC_MODEL,
    )
    assert ELEVENLABS_MODEL == "eleven_v3"
    assert SYNC_MODEL == "sync-3"
    assert DEFAULT_LIPSYNC_ENGINE == "syncso"
    # The ONLY EvoLink Seedance model that accepts audio_urls (i2v 400s).
    assert NATIVE_LIPSYNC_MODEL == "seedance-2.0-reference-to-video"


def test_load_settings_gemini_optional(monkeypatch):
    monkeypatch.setenv("EVOLINK_API_KEY", "evo-abc")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = load_settings()
    assert s.gemini_api_key == ""


def test_load_settings_reads_anthropic_key_and_llm_provider(monkeypatch):
    monkeypatch.setenv("EVOLINK_API_KEY", "evo-abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-xyz")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    s = load_settings()
    assert s.anthropic_api_key == "ant-xyz"
    assert s.llm_provider == "gemini"


def test_load_settings_llm_provider_defaults_to_claude(monkeypatch):
    monkeypatch.setenv("EVOLINK_API_KEY", "evo-abc")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = load_settings()
    assert s.llm_provider == "claude"
    assert s.anthropic_api_key == ""


def test_model_id_constants_exposed():
    from app.config import (
        GEMINI_FAST_MODEL, GEMINI_PRO_MODEL,
        GEMINI_FAST_FALLBACK, GEMINI_PRO_FALLBACK,
        NB_PRO_MODEL, NB2_MODEL,
    )
    assert GEMINI_FAST_MODEL == "gemini-3.5-flash"
    assert GEMINI_PRO_MODEL == "gemini-3.1-pro-preview"
    assert GEMINI_FAST_FALLBACK == "gemini-2.5-flash"
    assert GEMINI_PRO_FALLBACK == "gemini-2.5-pro"
    assert NB_PRO_MODEL == "gemini-3-pro-image-preview"
    assert NB2_MODEL == "gemini-3.1-flash-image-preview"


def test_claude_model_constants_exposed():
    from app.config import (
        CLAUDE_FAST_MODEL, CLAUDE_LITE_MODEL, CLAUDE_PRO_MODEL,
        DEFAULT_LLM_PROVIDER,
    )
    assert CLAUDE_PRO_MODEL == "claude-opus-4-8"
    assert CLAUDE_FAST_MODEL == "claude-sonnet-4-6"
    assert CLAUDE_LITE_MODEL == "claude-haiku-4-5"
    assert DEFAULT_LLM_PROVIDER == "claude"
