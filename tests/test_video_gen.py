from enum import Enum
from types import SimpleNamespace
import time
import asyncio

import pytest

from app.config import KLING_BROLL_MODEL, OMNI_VIDEO_MODEL, Settings
from app.video_gen import (
    DEFAULT_MODEL_ID,
    EVOLINK_BASE,
    EvoLinkSeedanceGenerator,
    EvoLinkVideoClient,
    OMNI_EVOLINK_MODELS,
    OmniVideoClient,
    VideoRequest,
    _kling_quality,
    _omni_evolink_body,
    _omni_interaction_kwargs,
    make_video_client,
    provider_for_model,
)


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


def _request(**overrides):
    base = dict(
        prompt="a woman waves at the camera",
        first_frame_url="https://ff/a.png",
        last_frame_url="https://lf/a.png",
        duration=8,
        aspect_ratio="9:16",
        resolution="720p",
        generate_audio=True,
        seed=None,
    )
    base.update(overrides)
    return VideoRequest(**base)


def test_create_task_posts_evolink_flf2v_body():
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["url"] = url
        seen["body"] = body
        seen["headers"] = headers
        return {"id": "task-123", "status": "processing"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)

    task_id, payload = client.create_task(_request())

    assert task_id == "task-123"
    assert seen["url"] == f"{EVOLINK_BASE}/videos/generations"
    assert seen["headers"]["Authorization"] == "Bearer evo-xxx"
    assert seen["body"] == {
        "model": DEFAULT_MODEL_ID,
        "prompt": "a woman waves at the camera",
        "image_urls": ["https://ff/a.png", "https://lf/a.png"],
        "duration": 8,
        "quality": "720p",
        "aspect_ratio": "9:16",
        "generate_audio": True,
    }
    assert payload["task_id"] == "task-123"


def test_single_image_request_only_sends_first_frame():
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = body
        return {"id": "t-1"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)
    client.create_task(_request(last_frame_url=None))

    assert seen["body"]["image_urls"] == ["https://ff/a.png"]


def test_single_image_silent_clip_body():
    """Talking-head clips: one image, no audio (lipsynced separately)."""
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = body
        return {"id": "t-1"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)
    client.create_task(_request(
        last_frame_url=None, generate_audio=False, duration=5,
    ))

    body = seen["body"]
    assert body["image_urls"] == ["https://ff/a.png"]   # single image
    assert body["generate_audio"] is False               # silent
    assert body["duration"] == 5


def test_camera_fixed_emitted_only_when_true():
    """False is the documented API default — the key is sent only when the
    camera is actually locked, so unverified models (r2v) never receive it."""
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = body
        return {"id": "t-1"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)

    client.create_task(_request(camera_fixed=True))
    assert seen["body"]["camera_fixed"] is True

    client.create_task(_request(camera_fixed=False))
    assert "camera_fixed" not in seen["body"]


def test_audio_url_sent_as_audio_urls_list():
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = body
        return {"id": "t-1"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)
    client.create_task(_request(audio_url="https://au/ref.mp3"))

    assert seen["body"]["audio_urls"] == ["https://au/ref.mp3"]


def test_no_audio_url_omits_audio_urls_key():
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = body
        return {"id": "t-1"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)
    client.create_task(_request(audio_url=None))

    assert "audio_urls" not in seen["body"]


def test_seed_included_when_provided():
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = body
        return {"id": "t-1"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)
    client.create_task(_request(seed=42))

    assert seen["body"]["seed"] == 42


def test_poll_completes_with_video_url():
    poll_responses = iter([
        {"status": "processing"},
        {"status": "processing"},
        {"status": "completed", "results": ["https://v/output.mp4"]},
    ])

    def fake_post(*a, **kw):
        return {"id": "task-99"}

    def fake_get(url, headers, timeout):
        return next(poll_responses)

    client = EvoLinkSeedanceGenerator(
        _settings(), poll_interval_seconds=0,
        _post=fake_post, _get=fake_get,
    )
    statuses = []
    final = None
    for status, result in client.poll_task_stream("task-99"):
        statuses.append(status)
        if result is not None:
            final = result

    assert statuses == ["processing", "processing", "completed"]
    assert final is not None
    assert final.video_url == "https://v/output.mp4"
    assert final.task_id == "task-99"


def test_poll_raises_on_failed_status():
    def fake_post(*a, **kw):
        return {"id": "t-1"}

    def fake_get(url, headers, timeout):
        return {"status": "failed", "error": "moderation rejected the reference image"}

    client = EvoLinkSeedanceGenerator(
        _settings(), poll_interval_seconds=0,
        _post=fake_post, _get=fake_get,
    )
    with pytest.raises(RuntimeError, match="moderation"):
        list(client.poll_task_stream("t-1"))


def test_results_with_object_shape_extracts_url():
    """EvoLink sometimes returns results as [{url: ...}] instead of [str]."""
    def fake_post(*a, **kw):
        return {"id": "t-1"}

    def fake_get(url, headers, timeout):
        return {"status": "completed",
                "results": [{"url": "https://v/object.mp4"}]}

    client = EvoLinkSeedanceGenerator(
        _settings(), poll_interval_seconds=0,
        _post=fake_post, _get=fake_get,
    )
    final = None
    for _status, r in client.poll_task_stream("t-1"):
        if r is not None:
            final = r
    assert final.video_url == "https://v/object.mp4"


def test_result_object_without_url_field_raises():
    """If EvoLink returns a result dict but with no url-like key, we must
    raise rather than silently producing an empty video_url."""
    def fake_post(*a, **kw):
        return {"id": "t-1"}

    def fake_get(url, headers, timeout):
        return {"status": "completed", "results": [{"foo": "bar"}]}

    client = EvoLinkSeedanceGenerator(
        _settings(), poll_interval_seconds=0,
        _post=fake_post, _get=fake_get,
    )
    with pytest.raises(RuntimeError, match="no url field"):
        list(client.poll_task_stream("t-1"))


@pytest.mark.asyncio
async def test_poll_async_raises_on_deadline():
    """A task stuck in 'processing' forever must hit the poll deadline and
    raise, not loop indefinitely and hang the whole gather."""
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "processing"}

    class _FakeAsyncClient:
        async def get(self, *a, **kw):
            return _Resp()

    client = EvoLinkSeedanceGenerator(
        _settings(), poll_interval_seconds=0, _async_client=_FakeAsyncClient(),
    )
    with pytest.raises(RuntimeError, match="deadline"):
        async for _status, _r in client.poll_task_stream_async(
            "t-1", max_wait_seconds=0.0,
        ):
            pass


def test_missing_task_id_raises():
    def fake_post(*a, **kw):
        return {"status": "error"}  # no id

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)
    with pytest.raises(RuntimeError, match="missing task id"):
        client.create_task(_request())


# ─── provider seam: Seedance vs Kling body shapes ────────────────────

def _sent_body(**overrides) -> dict:
    """Build a request, post it through a fake, return the body we'd have sent."""
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = body
        return {"id": "t-1"}

    client = EvoLinkSeedanceGenerator(_settings(), _post=fake_post)
    client.create_task(_request(**overrides))
    return seen["body"]


def test_seedance_body_is_unchanged_by_the_provider_split():
    """Regression guard: splitting _build_body into a per-provider dispatch was
    a refactor, so the Seedance body — including every optional key — must be
    byte-for-byte what we sent before Kling existed."""
    body = _sent_body(camera_fixed=True, seed=42, audio_url="https://au/ref.mp3")

    assert body == {
        "model": DEFAULT_MODEL_ID,
        "prompt": "a woman waves at the camera",
        "image_urls": ["https://ff/a.png", "https://lf/a.png"],
        "duration": 8,
        "quality": "720p",
        "aspect_ratio": "9:16",
        "generate_audio": True,
        "camera_fixed": True,
        "audio_urls": ["https://au/ref.mp3"],
        "seed": 42,
    }


def test_kling_body_shape():
    """Kling takes image_start (not image_urls), an INTEGER duration, and a
    STRING sound enum — a boolean or an image_urls list is a 400 on a paid call."""
    body = _sent_body(
        model=KLING_BROLL_MODEL,
        negative_prompt="scene cut, camera shake",
    )

    assert body == {
        "model": KLING_BROLL_MODEL,
        "image_start": "https://ff/a.png",
        "image_end": "https://lf/a.png",
        "prompt": "a woman waves at the camera",
        "duration": 8,
        "quality": "720p",
        "sound": "off",
        "negative_prompt": "scene cut, camera shake",
    }
    assert isinstance(body["duration"], int)
    assert body["sound"] == "off"        # string enum, not False


def test_kling_body_omits_optional_keys_when_unset():
    body = _sent_body(model=KLING_BROLL_MODEL, last_frame_url=None)

    assert "image_end" not in body
    assert "negative_prompt" not in body
    assert body["image_start"] == "https://ff/a.png"


def test_kling_body_never_emits_seedance_only_keys():
    """Kling i2v has no seed (non-reproducible), no camera_fixed, no
    aspect_ratio (derived from image_start) and no generate_audio. Sending an
    unknown key risks rejecting a paid request."""
    body = _sent_body(
        model=KLING_BROLL_MODEL, seed=42, camera_fixed=True, generate_audio=True,
    )

    for key in ("seed", "camera_fixed", "aspect_ratio",
                "generate_audio", "image_urls"):
        assert key not in body


def test_kling_body_rejects_audio_url():
    """Kling has no audio-driven generation, so routing an audio-anchored beat
    (seedance_native background_vo) here is a programming error, not a retry."""
    with pytest.raises(ValueError, match="audio-driven"):
        _sent_body(model=KLING_BROLL_MODEL, audio_url="https://au/ref.mp3")


def test_kling_quality_clamps_480p_up_to_720p():
    """Kling's floor is 720p — there is no 480p tier — and 720p on Kling is
    still cheaper per second than 480p on Seedance."""
    assert _kling_quality("480p") == "720p"


@pytest.mark.parametrize("res", ["720p", "1080p", "4k"])
def test_kling_quality_passes_supported_tiers(res):
    assert _kling_quality(res) == res


def test_kling_quality_rejects_unknown_resolution():
    with pytest.raises(ValueError, match="not a Kling quality"):
        _kling_quality("2160p")


def test_provider_for_model_maps_both_families():
    assert provider_for_model(DEFAULT_MODEL_ID) == "seedance"
    assert provider_for_model("seedance-2.0-reference-to-video") == "seedance"
    assert provider_for_model(KLING_BROLL_MODEL) == "kling"
    assert provider_for_model("kling-v3-turbo-image-to-video") == "kling"


def test_provider_for_model_raises_on_unknown_slug():
    """Fail fast: a body built with the wrong schema is a silently-rejected
    PAID request, so an unrecognised family must never fall through."""
    with pytest.raises(ValueError, match="unknown EvoLink model family"):
        provider_for_model("veo-3-image-to-video")


# ─── Omni (gemini-omni-flash-preview, Interactions API) ──────────────

def _omni_settings(**overrides):
    base = dict(
        evolink_api_key="evo-xxx",
        aws_access_key_id="akid",
        aws_secret_access_key="secret",
        aws_bucket_name="bucket",
        aws_region="ap-south-1",
        gemini_api_key="gem-xxx",
        elevenlabs_api_key="el-xxx",
        sync_api_key="sy-xxx",
    )
    base.update(overrides)
    return Settings(**base)


def _omni_request(**overrides):
    base = dict(
        prompt="a woman waves at the camera",
        first_frame_url="https://ff/a.png",
        last_frame_url=None,
        duration=6,
        aspect_ratio="9:16",
        resolution="720p",
        generate_audio=False,
        seed=None,
        model=OMNI_VIDEO_MODEL,
    )
    base.update(overrides)
    return VideoRequest(**base)


class _FakeInteraction:
    def __init__(self, id, status, output_video=None):
        self.id = id
        self.status = status
        self.output_video = output_video


class _FakeOutputVideo:
    def __init__(self, uri):
        self.uri = uri


class _FakeInteractions:
    """Records .create/.get calls; scripted per-call outcomes."""
    def __init__(self, create_side_effects=None, get_sequence=None):
        self.create_calls = []
        self.get_calls = []
        self._create_side_effects = list(create_side_effects or [])
        self._get_sequence = list(get_sequence or [])

    def create(self, **kwargs):
        import copy
        # Deep-copy: the client mutates the SAME kwargs dict in place on
        # retry (pops "duration" then reuses it), so a shallow record would
        # make both calls look identical after the fact.
        self.create_calls.append(copy.deepcopy(kwargs))
        if self._create_side_effects:
            outcome = self._create_side_effects.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return _FakeInteraction("omni-task-1", "queued")

    def get(self, task_id):
        self.get_calls.append(task_id)
        return self._get_sequence.pop(0)


class _FakeFiles:
    def __init__(self, video_bytes=b"omni-video-bytes"):
        self.download_calls = []
        self.video_bytes = video_bytes

    def download(self, *, file):
        self.download_calls.append(file)
        return self.video_bytes


class _FakeOmniSDK:
    def __init__(self, create_side_effects=None, get_sequence=None,
                 video_bytes=b"omni-video-bytes"):
        self.interactions = _FakeInteractions(create_side_effects, get_sequence)
        self.files = _FakeFiles(video_bytes)


class _FakeUploader:
    def __init__(self, url="https://r2/omni-clip.mp4"):
        self.url = url
        self.upload_calls = []

    def upload(self, path):
        self.upload_calls.append(path)
        return self.url


class _ClientError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


async def _fake_fetch_image(url):
    return b"img-bytes", "image/jpeg"


def _omni_client(sdk=None, uploader=None, fetch_image=None, poll_interval_seconds=0):
    return OmniVideoClient(
        _omni_settings(),
        _client=sdk if sdk is not None else _FakeOmniSDK(),
        _uploader=uploader if uploader is not None else _FakeUploader(),
        _fetch_image=fetch_image if fetch_image is not None else _fake_fetch_image,
        poll_interval_seconds=poll_interval_seconds,
    )


# ── _omni_interaction_kwargs (request shape) ─────────────────────────

def test_omni_interaction_kwargs_exact_shape():
    request = _omni_request(seed=42)
    kwargs = _omni_interaction_kwargs(request, "aW1nYmFzZTY0", "image/jpeg")

    assert kwargs == {
        "model": OMNI_VIDEO_MODEL,
        "input": [
            {"type": "image", "data": "aW1nYmFzZTY0", "mime_type": "image/jpeg"},
            {"type": "text", "text": "a woman waves at the camera"},
        ],
        "generation_config": {"video_config": {"task": "image_to_video"}, "seed": 42},
        "response_format": {
            "type": "video",
            "aspect_ratio": "9:16",
            "duration": "6s",
            "delivery": "uri",
        },
        "background": True,
    }


def test_omni_interaction_kwargs_omits_seed_when_none():
    request = _omni_request(seed=None)
    kwargs = _omni_interaction_kwargs(request, "aW1n", "image/jpeg")
    assert "seed" not in kwargs["generation_config"]


@pytest.mark.parametrize("duration,expected", [(1, 3), (2, 3), (30, 10), (11, 10)])
def test_omni_interaction_kwargs_clamps_duration(duration, expected):
    request = _omni_request(duration=duration)
    kwargs = _omni_interaction_kwargs(request, "aW1n", "image/jpeg")
    assert kwargs["response_format"]["duration"] == f"{expected}s"


def test_omni_interaction_kwargs_rejects_audio_url():
    request = _omni_request(audio_url="https://au/ref.mp3")
    with pytest.raises(ValueError, match="no audio input"):
        _omni_interaction_kwargs(request, "aW1n", "image/jpeg")


def test_omni_interaction_kwargs_rejects_last_frame_url():
    request = _omni_request(last_frame_url="https://lf/a.png")
    with pytest.raises(ValueError, match="no last-frame control"):
        _omni_interaction_kwargs(request, "aW1n", "image/jpeg")


def test_omni_interaction_kwargs_rejects_bad_aspect_ratio():
    request = _omni_request(aspect_ratio="1:1")
    with pytest.raises(ValueError, match="9:16 and 16:9"):
        _omni_interaction_kwargs(request, "aW1n", "image/jpeg")


# ── OmniVideoClient.create_task_async ────────────────────────────────

@pytest.mark.asyncio
async def test_omni_create_task_returns_id_and_payload_without_base64():
    sdk = _FakeOmniSDK(create_side_effects=[_FakeInteraction("omni-1", "queued")])
    client = _omni_client(sdk=sdk)

    task_id, payload = await client.create_task_async(_omni_request())

    assert task_id == "omni-1"
    assert payload["task_id"] == "omni-1"
    assert "input" not in payload["request"]
    assert payload["request"]["prompt"] == "a woman waves at the camera"
    assert payload["request"]["first_frame_url"] == "https://ff/a.png"
    # image was actually fetched and base64-encoded into the SDK call, just not
    # carried into the persisted payload.
    sent = sdk.interactions.create_calls[0]
    assert sent["input"][0]["data"]  # non-empty base64 string


@pytest.mark.asyncio
async def test_omni_create_task_retries_once_on_duration_400():
    err = _ClientError("Invalid field: duration must match ^[0-9]+s$", status_code=400)
    sdk = _FakeOmniSDK(create_side_effects=[
        err, _FakeInteraction("omni-2", "queued"),
    ])
    client = _omni_client(sdk=sdk)

    task_id, payload = await client.create_task_async(_omni_request())

    assert task_id == "omni-2"
    assert len(sdk.interactions.create_calls) == 2
    assert "duration" in sdk.interactions.create_calls[0]["response_format"]
    # Retry dropped the duration field from the second attempt.
    assert "duration" not in sdk.interactions.create_calls[1]["response_format"]
    # And the downgrade is STICKY + visible: the rest of the run's clips must
    # neither repeat the rejected create nor be billed at the duration we asked
    # for (the API renders its 10s default instead).
    assert client.duration_supported is False


@pytest.mark.asyncio
async def test_omni_duration_downgrade_is_sticky_for_later_clips():
    err = _ClientError("Invalid field: duration", status_code=400)
    sdk = _FakeOmniSDK(create_side_effects=[
        err, _FakeInteraction("omni-2", "queued"),
        _FakeInteraction("omni-3", "queued"),
    ])
    client = _omni_client(sdk=sdk)

    await client.create_task_async(_omni_request())
    task_id, _payload = await client.create_task_async(_omni_request())

    assert task_id == "omni-3"
    # Three creates total, NOT four: the second clip skipped the doomed attempt.
    assert len(sdk.interactions.create_calls) == 3
    assert "duration" not in sdk.interactions.create_calls[2]["response_format"]


# ── OmniVideoClient.poll_task_stream_async ───────────────────────────

@pytest.mark.asyncio
async def test_omni_poll_stream_completes_with_uploaded_url():
    uploader = _FakeUploader(url="https://r2/omni-final.mp4")
    sdk = _FakeOmniSDK(get_sequence=[
        _FakeInteraction("omni-1", "in_progress"),
        _FakeInteraction("omni-1", "completed",
                         output_video=_FakeOutputVideo("gemini://files/abc")),
    ])
    client = _omni_client(sdk=sdk, uploader=uploader)

    statuses = []
    final = None
    async for status, result in client.poll_task_stream_async(
        "omni-1", max_wait_seconds=5.0,
    ):
        statuses.append(status)
        if result is not None:
            final = result

    assert statuses == ["processing", "completed"]
    assert final is not None
    assert final.video_url == "https://r2/omni-final.mp4"
    assert sdk.files.download_calls == ["gemini://files/abc"]
    assert uploader.upload_calls  # the downloaded bytes were uploaded


class _StatusEnum(str, Enum):
    """Mimics google-genai's CaseInSensitiveEnum members: str() is
    "ClassName.MEMBER", not the wire value."""
    COMPLETED = "completed"


@pytest.mark.parametrize("raw", [
    _StatusEnum.COMPLETED,        # str() -> "_StatusEnum.COMPLETED"
    "COMPLETED",
    "InteractionStatus.COMPLETED",
    "succeeded",
])
@pytest.mark.asyncio
async def test_omni_poll_stream_recognizes_terminal_status_variants(raw):
    """The status vocabulary is undocumented and the SDK returns enum members,
    so a fully-billed clip must not spin to the poll deadline just because its
    terminal state arrived as an enum repr / upper case / a synonym."""
    sdk = _FakeOmniSDK(get_sequence=[
        _FakeInteraction("omni-1", raw,
                         output_video=_FakeOutputVideo("gemini://files/abc")),
    ])
    client = _omni_client(sdk=sdk)

    seen = [(status, result) async for status, result
            in client.poll_task_stream_async("omni-1", max_wait_seconds=5.0)]

    assert [s for s, _r in seen] == ["completed"]
    assert seen[0][1].video_url == "https://r2/omni-clip.mp4"


@pytest.mark.asyncio
async def test_omni_probe_status_reports_without_downloading_anything():
    """Resume's pre-commit preview is documented as free and side-effect-free.
    The poller cannot serve it (it delivers the artifact before it can yield
    "completed"), so the probe reads the interaction record only."""
    uploader = _FakeUploader()
    sdk = _FakeOmniSDK(get_sequence=[
        _FakeInteraction("omni-1", "COMPLETED",
                         output_video=_FakeOutputVideo("gemini://files/abc")),
        _FakeInteraction("omni-2", "in_progress"),
    ])
    client = _omni_client(sdk=sdk, uploader=uploader)

    assert await client.probe_status_async("omni-1") == ("completed", True)
    assert await client.probe_status_async("omni-2") == ("in_progress", False)
    # Nothing was transferred: no Files download, no R2 write.
    assert sdk.files.download_calls == []
    assert uploader.upload_calls == []


@pytest.mark.asyncio
async def test_omni_poll_stream_raises_on_failed_status():
    sdk = _FakeOmniSDK(get_sequence=[_FakeInteraction("omni-1", "failed")])
    client = _omni_client(sdk=sdk)

    with pytest.raises(RuntimeError, match="task failed"):
        async for _status, _result in client.poll_task_stream_async(
            "omni-1", max_wait_seconds=5.0,
        ):
            pass


@pytest.mark.asyncio
async def test_omni_poll_stream_raises_on_deadline():
    """Stuck in in_progress forever must hit the poll deadline, not hang."""
    sdk = _FakeOmniSDK(get_sequence=[
        _FakeInteraction("omni-1", "in_progress"),
        _FakeInteraction("omni-1", "in_progress"),
        _FakeInteraction("omni-1", "in_progress"),
    ])
    client = _omni_client(sdk=sdk)
    client._poll_interval = 0

    with pytest.raises(RuntimeError, match="deadline"):
        async for _status, _result in client.poll_task_stream_async(
            "omni-1", max_wait_seconds=0.0,
        ):
            pass


# ── OmniVideoClient construction / factory routing ───────────────────

def test_omni_client_requires_gemini_api_key():
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        OmniVideoClient(_omni_settings(gemini_api_key=""), _client=_FakeOmniSDK(),
                        _uploader=_FakeUploader(), _fetch_image=_fake_fetch_image)


def test_make_video_client_routes_seedance_by_default():
    client = make_video_client(_omni_settings())
    assert isinstance(client, EvoLinkVideoClient)
    assert not isinstance(client, OmniVideoClient)


def test_make_video_client_routes_omni(monkeypatch):
    """genai.Client construction with a dummy key must not hit the network —
    the SDK builds a lazy HTTP client under the hood, so this only fails if a
    request is actually issued (none is, at construction time)."""
    client = make_video_client(_omni_settings(), "omni")
    assert isinstance(client, OmniVideoClient)


def test_make_video_client_rejects_unknown_generator():
    with pytest.raises(ValueError, match="unknown video_generator"):
        make_video_client(_omni_settings(), "veo")


# ── EvoLink's Omni resale (the fallback route's request shape) ────────
# Verified against evolink.ai/docs .../gemini-omni-flash-image-to-video
# (2026-08-05): model + prompt + image_urls (exactly one) + duration (int 3-10
# or "auto") + aspect_ratio (16:9 | 9:16 | auto) + callback_url, and nothing
# else. These tests are the guard against a field creeping back in from the
# Seedance body and 400-ing a PAID clip.

def test_omni_evolink_body_exact_shape():
    request = _request(model="gemini-omni-flash-image-to-video",
                       first_frame_url="https://cdn/anchor-0.png",
                       last_frame_url=None, duration=4, aspect_ratio="9:16")
    assert _omni_evolink_body(request) == {
        "model": "gemini-omni-flash-image-to-video",
        "prompt": request.prompt,
        "image_urls": ["https://cdn/anchor-0.png"],
        "aspect_ratio": "9:16",
        "duration": 4,
    }


def test_omni_evolink_body_drops_every_field_the_route_has_no_analogue_for():
    """seed / resolution / generate_audio / camera_fixed / negative_prompt all
    exist on the VideoRequest and NONE of them exist on EvoLink's Omni surface
    (audio is generated unconditionally; 720p is fixed)."""
    request = _request(model="gemini-omni-flash-image-to-video",
                       last_frame_url=None, seed=1234, resolution="1080p", generate_audio=True,
                       camera_fixed=True, negative_prompt="no text")
    body = _omni_evolink_body(request)
    for absent in ("seed", "quality", "resolution", "generate_audio",
                   "camera_fixed", "negative_prompt", "image_url"):
        assert absent not in body


@pytest.mark.parametrize("duration,expected", [(1, 3), (3, 3), (10, 10), (15, 10)])
def test_omni_evolink_body_clamps_duration_to_the_documented_range(duration, expected):
    request = _request(model="gemini-omni-flash-image-to-video",
                       last_frame_url=None, duration=duration)
    assert _omni_evolink_body(request)["duration"] == expected


def test_omni_evolink_body_sends_exactly_one_image_even_with_a_last_frame():
    """image_urls is min 1 / max 1 on this model, so a last-frame anchor cannot
    be appended — it must raise rather than silently ship a 2-item array."""
    request = _request(model="gemini-omni-flash-image-to-video",
                       last_frame_url="https://cdn/anchor-0-end.png")
    with pytest.raises(ValueError, match="last-frame"):
        _omni_evolink_body(request)


def test_omni_evolink_body_rejects_audio_url():
    request = _request(model="gemini-omni-flash-image-to-video",
                       last_frame_url=None,
                       audio_url="https://cdn/beat-0.wav")
    with pytest.raises(ValueError, match="no audio input"):
        _omni_evolink_body(request)


def test_omni_evolink_body_rejects_bad_aspect_ratio():
    request = _request(model="gemini-omni-flash-image-to-video",
                       last_frame_url=None, aspect_ratio="1:1")
    with pytest.raises(ValueError, match="9:16, 16:9 and auto"):
        _omni_evolink_body(request)


def test_omni_evolink_body_allows_auto_aspect_ratio():
    request = _request(model="gemini-omni-flash-image-to-video",
                       last_frame_url=None, aspect_ratio="auto")
    assert _omni_evolink_body(request)["aspect_ratio"] == "auto"


@pytest.mark.parametrize("model", OMNI_EVOLINK_MODELS)
def test_provider_for_model_maps_every_evolink_omni_slug(model):
    assert provider_for_model(model) == "omni_evolink"


def test_provider_for_model_still_rejects_googles_own_omni_slug():
    """gemini-omni-flash-preview is the INTERACTIONS-API model — it never rides
    the EvoLink body dispatch, and mistaking it for one would build a body for
    the wrong API on a paid call."""
    with pytest.raises(ValueError, match="unknown EvoLink model family"):
        provider_for_model(OMNI_VIDEO_MODEL)


@pytest.mark.asyncio
async def test_omni_creates_are_capped_but_polls_are_not(monkeypatch):
    """The clip stage gathers every beat at once, so a 24-beat run fired 24
    creates in the same instant (2026-08-06) at a surface whose documented
    sibling limit is ~10 req/min. Creates are gated; POLLS deliberately are not —
    a poll that cannot run is a clip nobody is watching while its 20-minute
    deadline keeps ticking.
    """
    import app.video_gen as vg

    class _CountingInteractions:
        def __init__(self):
            self.in_flight = 0
            self.peak = 0

        def create(self, **kwargs):
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            time.sleep(0.02)          # real thread overlap, not a fake await
            self.in_flight -= 1
            return SimpleNamespace(id="v1_task", status="queued")

    class _SDK:
        def __init__(self):
            self.interactions = _CountingInteractions()

    monkeypatch.setattr(vg, "OMNI_MAX_CONCURRENCY", 3)
    sdk = _SDK()
    client = OmniVideoClient(_omni_settings(), _client=sdk,
                             _uploader=_FakeUploader(),
                             _fetch_image=_fake_fetch_image)
    req = _request(model=OMNI_VIDEO_MODEL, last_frame_url=None)
    await asyncio.gather(*[client.create_task_async(req) for _ in range(12)])

    assert sdk.interactions.peak <= 3, (
        f"{sdk.interactions.peak} creates were in flight at once")
