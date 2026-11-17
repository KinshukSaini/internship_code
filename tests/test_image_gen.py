import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import NB2_MODEL, NB_PRO_MODEL, Settings
from app.image_gen import NanoBananaImageGenerator


def _settings():
    return Settings(
        evolink_api_key="evo-xxx",
        aws_access_key_id="akid",
        aws_secret_access_key="secret",
        aws_bucket_name="bucket",
        aws_region="ap-south-1",
        gemini_api_key="gem-xxx",
        elevenlabs_api_key="el-xxx",
        sync_api_key="sy-xxx",
    )


def _fake_uploader(url="https://s3/bucket/key.jpg"):
    up = MagicMock()
    up.upload.return_value = url
    return up


def _fake_genai_client_with_image(image_bytes: bytes = b"fake-png-bytes"):
    """Build a MagicMock that mimics google-genai's Client API surface.

    Both sync (client.models.generate_content) and async
    (client.aio.models.generate_content) return a response whose
    candidates[0].content.parts[0].inline_data carries our fake bytes.
    """
    inline = MagicMock(data=image_bytes, mime_type="image/jpeg")
    part = MagicMock(inline_data=inline)
    content = MagicMock(parts=[part])
    cand = MagicMock(content=content)
    resp = MagicMock(candidates=[cand])

    fake = MagicMock()
    fake.models.generate_content = MagicMock(return_value=resp)
    fake.aio.models.generate_content = AsyncMock(return_value=resp)
    return fake, fake.models.generate_content, fake.aio.models.generate_content


def test_anchor_tier_uses_nb2_with_thinking_high():
    fake, sync_call, _ = _fake_genai_client_with_image()
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    images = gen.generate_sync(prompt="hi", tier="anchor")

    assert len(images) == 1
    kw = sync_call.call_args.kwargs
    assert kw["model"] == NB2_MODEL
    cfg = kw["config"]
    assert cfg.image_config.aspect_ratio == "9:16"
    assert cfg.image_config.image_size == "2K"
    # NB2 must include thinking_config with thinking_level="high".
    # The SDK normalizes the string into a ThinkingLevel enum whose value
    # is "HIGH" — match either form.
    assert cfg.thinking_config is not None
    assert str(cfg.thinking_config.thinking_level).lower().endswith("high")


def test_hero_tier_uses_nb_pro_without_thinking():
    fake, sync_call, _ = _fake_genai_client_with_image()
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    gen.generate_sync(prompt="hi", tier="hero")

    kw = sync_call.call_args.kwargs
    assert kw["model"] == NB_PRO_MODEL
    # NB Pro must NOT send thinking_config
    assert kw["config"].thinking_config is None


def test_returned_image_has_public_url_from_uploader():
    fake, _, _ = _fake_genai_client_with_image()
    up = _fake_uploader(url="https://s3/the/key.jpg")
    gen = NanoBananaImageGenerator(_settings(), up, _client=fake)

    images = gen.generate_sync(prompt="hi", tier="anchor")

    assert images[0].url == "https://s3/the/key.jpg"
    assert images[0].prompt == "hi"
    assert images[0].size == "2K"
    up.upload.assert_called_once()


def test_aspect_ratio_override_propagates():
    fake, sync_call, _ = _fake_genai_client_with_image()
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    gen.generate_sync(prompt="hi", tier="anchor", aspect_ratio="16:9", size="4K")

    cfg = sync_call.call_args.kwargs["config"]
    assert cfg.image_config.aspect_ratio == "16:9"
    assert cfg.image_config.image_size == "4K"


def test_n_generates_multiple_images():
    fake, sync_call, _ = _fake_genai_client_with_image()
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    images = gen.generate_sync(prompt="hi", tier="anchor", n=3)

    assert len(images) == 3
    assert sync_call.call_count == 3


def test_empty_candidates_raises_clear_error():
    """Safety refusals must surface as a clear error, not silently return []."""
    resp = MagicMock(candidates=[])
    fake = MagicMock()
    fake.models.generate_content = MagicMock(return_value=resp)
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    with pytest.raises(RuntimeError, match="no image candidates"):
        gen.generate_sync(prompt="hi", tier="anchor")


def test_text_only_candidate_raises_clear_error():
    """A candidate with no inline_data must also raise."""
    part = MagicMock(inline_data=None)
    content = MagicMock(parts=[part])
    cand = MagicMock(content=content)
    resp = MagicMock(candidates=[cand])

    fake = MagicMock()
    fake.models.generate_content = MagicMock(return_value=resp)
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    with pytest.raises(RuntimeError, match="no image candidates"):
        gen.generate_sync(prompt="hi", tier="anchor")


@pytest.mark.asyncio
async def test_async_path_uses_aio_models():
    """generate_async must hit client.aio.models.generate_content, not sync path."""
    fake, sync_call, async_call = _fake_genai_client_with_image()
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    images = await gen.generate_async(prompt="hi", tier="anchor")

    assert len(images) == 1
    async_call.assert_awaited_once()
    sync_call.assert_not_called()


@pytest.mark.asyncio
async def test_product_only_citation_is_not_character():
    """THE BUG: a product-only B-roll ref must NOT be cited as the character.

    Previously a single ref always got the 'IMAGE 1 IS THE CHARACTER … copy
    the face pixel-for-pixel' citation — so a product pouch was described as a
    person. With reference_roles=['product'] it must use the product citation.
    """
    from app.image_gen import _REF_BYTES_CACHE, NanoBananaImageGenerator
    url = "https://s3/product-pouch.jpg"
    _REF_BYTES_CACHE[url] = (b"png", "image/png")   # prime cache → no network

    parts = await NanoBananaImageGenerator._build_contents(
        "B-roll of the product on a sunlit counter", [url],
        reference_roles=["product"],
    )
    citation = parts[0]
    assert "IMAGE 1 IS THE CHARACTER" not in citation
    assert "IMAGE 1 IS THE PRODUCT" in citation


@pytest.mark.asyncio
async def test_character_only_citation():
    from app.image_gen import _REF_BYTES_CACHE, NanoBananaImageGenerator
    url = "https://s3/char.jpg"
    _REF_BYTES_CACHE[url] = (b"png", "image/png")
    parts = await NanoBananaImageGenerator._build_contents(
        "talking head", [url], reference_roles=["character"],
    )
    assert "IMAGE 1 IS THE CHARACTER" in parts[0]


@pytest.mark.asyncio
async def test_character_and_product_citation():
    from app.image_gen import _REF_BYTES_CACHE, NanoBananaImageGenerator
    cu, pu = "https://s3/char2.jpg", "https://s3/prod2.jpg"
    _REF_BYTES_CACHE[cu] = (b"png", "image/png")
    _REF_BYTES_CACHE[pu] = (b"png", "image/png")
    parts = await NanoBananaImageGenerator._build_contents(
        "creator holds the product", [cu, pu],
        reference_roles=["character", "product"],
    )
    citation = parts[0]
    assert "IMAGE 1 IS THE CHARACTER" in citation
    assert "IMAGE 2 IS THE PRODUCT" in citation


@pytest.mark.asyncio
async def test_hands_only_citation_matches_creator_no_face():
    """A hand-featuring B-roll cites the character ref for HANDS only — match
    her skin/hands, never render the face."""
    from app.image_gen import _REF_BYTES_CACHE, NanoBananaImageGenerator
    u = "https://s3/char-hands.jpg"
    _REF_BYTES_CACHE[u] = (b"png", "image/png")
    parts = await NanoBananaImageGenerator._build_contents(
        "hands pouring the product", [u], reference_roles=["character_hands"],
    )
    c = parts[0]
    assert "hands" in c.lower() and "Do NOT render her face" in c
    assert "IMAGE 1 IS THE CHARACTER" not in c   # not the face-matching citation


@pytest.mark.asyncio
async def test_hands_and_product_citation():
    from app.image_gen import _REF_BYTES_CACHE, NanoBananaImageGenerator
    h, p = "https://s3/h.jpg", "https://s3/p.jpg"
    _REF_BYTES_CACHE[h] = (b"png", "image/png")
    _REF_BYTES_CACHE[p] = (b"png", "image/png")
    parts = await NanoBananaImageGenerator._build_contents(
        "hands holding the product", [h, p],
        reference_roles=["character_hands", "product"],
    )
    c = parts[0]
    assert "hands" in c.lower() and "IMAGE 2 IS THE PRODUCT" in c
    assert "Do NOT render her face" in c


@pytest.mark.asyncio
async def test_product_source_citation_is_an_edit_not_a_pixel_lock():
    """product_source = clean up the user's own photo (drop the carton). It is
    an EDIT instruction, recognized before the 1-ref=character fallback, and
    must NOT carry the pixel-lock 'do not change container type' wording."""
    from app.image_gen import _REF_BYTES_CACHE, NanoBananaImageGenerator, _citation_for
    u = "https://s3/source-product.jpg"
    _REF_BYTES_CACHE[u] = (b"png", "image/png")
    parts = await NanoBananaImageGenerator._build_contents(
        "clean studio shot", [u], reference_roles=["product_source"],
    )
    c = parts[0]
    assert "IMAGE 1 IS THE SOURCE PRODUCT PHOTO" in c
    assert "REMOVE any carton, box, outer packaging" in c
    assert "ALL text unchanged" in c
    assert "IMAGE 1 IS THE CHARACTER" not in c
    # single ref + product_source must NOT fall through to the pixel-lock.
    direct = _citation_for(["product_source"], 1)
    assert "IMAGE 1 IS THE SOURCE PRODUCT PHOTO" in direct
    assert "do NOT change the container type" not in direct
    assert "do NOT change the packaging" not in direct


def test_product_citations_carry_no_box_insurance():
    """The three product citations must instruct rendering the item itself,
    not its retail box/outer packaging — and must be product-AGNOSTIC (no
    bottle/container/flavour bias that corrupts garment reproduction), and must
    forbid inventing branding the reference doesn't show."""
    from app.image_gen import (
        _CITATION_PRODUCT_ONLY, _CITATION_CHARACTER_AND_PRODUCT,
        _CITATION_HANDS_AND_PRODUCT,
    )
    for c in (_CITATION_PRODUCT_ONLY, _CITATION_CHARACTER_AND_PRODUCT,
              _CITATION_HANDS_AND_PRODUCT):
        assert "NOT any retail box or outer packaging" in c
        # no cosmetics/bottle-specific bias (breaks garment fidelity).
        low = c.lower()
        assert "bottle" not in low and "flavour" not in low \
            and "container type" not in low


def test_product_citations_are_leak_safe_and_drape_aware():
    """A live anchor rendered a garment tag literally reading 'PIXEL-ACCURATE /
    M / MADE IN C2OLAN' — the citation's own ALL-CAPS token, transcribed onto
    the product. Product citations must (a) never contain slogan-shaped caps
    tokens near label/text instructions, (b) explicitly forbid printing
    instruction words, and (c) tell the model worn garments drape naturally
    instead of reproducing the flat-lay reference silhouette."""
    from app.image_gen import (
        _CITATION_PRODUCT_ONLY, _CITATION_CHARACTER_AND_PRODUCT,
        _CITATION_HANDS_AND_PRODUCT, _CITATION_PRODUCT_SOURCE,
    )
    for c in (_CITATION_PRODUCT_ONLY, _CITATION_CHARACTER_AND_PRODUCT,
              _CITATION_HANDS_AND_PRODUCT, _CITATION_PRODUCT_SOURCE):
        assert "PIXEL-ACCURATE" not in c.upper()   # the leaked token, banned
        assert "Never print any words from these instructions" in c
    for c in (_CITATION_PRODUCT_ONLY, _CITATION_CHARACTER_AND_PRODUCT,
              _CITATION_HANDS_AND_PRODUCT):
        assert "drape naturally on the body" in c
        assert "folded or laid-flat silhouette" in c


@pytest.mark.asyncio
async def test_citation_legacy_fallback_without_roles():
    """No reference_roles (gallery tab) → legacy count convention preserved."""
    from app.image_gen import _REF_BYTES_CACHE, NanoBananaImageGenerator
    u = "https://s3/single.jpg"
    _REF_BYTES_CACHE[u] = (b"png", "image/png")
    parts = await NanoBananaImageGenerator._build_contents("x", [u])
    assert "IMAGE 1 IS THE CHARACTER" in parts[0]   # 1 ref → character (legacy)


@pytest.mark.asyncio
async def test_async_path_hero_tier_uses_nb_pro():
    fake, _, async_call = _fake_genai_client_with_image()
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    await gen.generate_async(prompt="hi", tier="hero")

    kw = async_call.call_args.kwargs
    assert kw["model"] == NB_PRO_MODEL
    assert kw["config"].thinking_config is None


# ─── model fallback on 5xx ──────────────────────────────────────────

def _server_error(code=503):
    from google.genai import errors
    return errors.ServerError(
        code, {"error": {"code": code, "message": "high demand",
                         "status": "UNAVAILABLE"}},
    )


def test_sync_503_falls_back_to_next_image_model():
    """Primary 503 → next model in IMAGE_FALLBACK_MODELS is tried."""
    from app.config import IMAGE_FALLBACK_MODELS
    fake, sync_call, _ = _fake_genai_client_with_image()
    good_resp = sync_call.return_value
    sync_call.side_effect = [_server_error(), good_resp]
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    images = gen.generate_sync(prompt="hi", tier="hero")

    assert sync_call.call_count == 2
    assert sync_call.call_args_list[0].kwargs["model"] == NB_PRO_MODEL
    assert sync_call.call_args_list[1].kwargs["model"] == IMAGE_FALLBACK_MODELS[0]
    assert len(images) == 1


@pytest.mark.asyncio
async def test_async_503_walks_full_chain_then_raises():
    """All models down → the last ServerError propagates, chain fully walked."""
    from google.genai import errors
    from app.config import IMAGE_FALLBACK_MODELS
    fake, _, async_call = _fake_genai_client_with_image()
    async_call.side_effect = [_server_error(), _server_error(), _server_error()]
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    with pytest.raises(errors.ServerError):
        await gen.generate_async(prompt="hi", tier="hero")

    tried = [c.kwargs["model"] for c in async_call.call_args_list]
    assert tried == [NB_PRO_MODEL, *IMAGE_FALLBACK_MODELS]


def test_client_error_does_not_fall_back():
    """4xx (bad request/auth/quota) fails fast — no model would fix it."""
    from google.genai import errors
    fake, sync_call, _ = _fake_genai_client_with_image()
    sync_call.side_effect = errors.ClientError(
        400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}},
    )
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    with pytest.raises(errors.ClientError):
        gen.generate_sync(prompt="hi", tier="anchor")

    assert sync_call.call_count == 1


def test_anchor_tier_fallback_skips_duplicate_of_primary():
    """The chain never repeats a model even if primary appears in fallbacks."""
    chain = NanoBananaImageGenerator._model_chain(NB2_MODEL)
    assert chain[0] == NB2_MODEL
    assert len(chain) == len(set(chain))


def _size_unsupported_error(size="2K"):
    from google.genai import errors
    return errors.ClientError(
        400, {"error": {"code": 400,
                        "message": f"Image size {size} is not supported for this model",
                        "status": "INVALID_ARGUMENT"}},
    )


def test_fallback_size_400_downgrades_to_1k_on_same_model():
    """Live failure 2026-07-14: Pro 503 → flash 500 → flash-lite 400
    'Image size 2K is not supported' killed the run. A size-unsupported 400
    on a fallback model must retry the SAME model at native 1K, not die."""
    from app.config import IMAGE_FALLBACK_MODELS
    fake, sync_call, _ = _fake_genai_client_with_image()
    good_resp = sync_call.return_value
    sync_call.side_effect = [_server_error(), _size_unsupported_error(), good_resp]
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    images = gen.generate_sync(prompt="hi", tier="hero")

    calls = sync_call.call_args_list
    assert [c.kwargs["model"] for c in calls] == [
        NB_PRO_MODEL, IMAGE_FALLBACK_MODELS[0], IMAGE_FALLBACK_MODELS[0],
    ]
    assert calls[1].kwargs["config"].image_config.image_size == "2K"
    assert calls[2].kwargs["config"].image_config.image_size == "1K"
    # metadata records the size that ACTUALLY rendered
    assert images[0].size == "1K"


def test_size_400_at_1k_fails_fast_no_loop():
    """Already at 1K → no further downgrade exists; raise, don't spin."""
    from google.genai import errors
    fake, sync_call, _ = _fake_genai_client_with_image()
    sync_call.side_effect = _size_unsupported_error("1K")
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    with pytest.raises(errors.ClientError):
        gen.generate_sync(prompt="hi", tier="anchor", size="1K")

    assert sync_call.call_count == 1


@pytest.mark.asyncio
async def test_async_fallback_size_400_downgrades_too():
    """The async path (the live pipeline's path) must repair size-400 the
    same way."""
    from app.config import IMAGE_FALLBACK_MODELS
    fake, _, async_call = _fake_genai_client_with_image()
    good_resp = async_call.return_value
    async_call.side_effect = [_server_error(), _size_unsupported_error(), good_resp]
    gen = NanoBananaImageGenerator(_settings(), _fake_uploader(), _client=fake)

    images = await gen.generate_async(prompt="hi", tier="hero")

    calls = async_call.call_args_list
    assert calls[-1].kwargs["model"] == IMAGE_FALLBACK_MODELS[0]
    assert calls[-1].kwargs["config"].image_config.image_size == "1K"
    assert images[0].size == "1K"
