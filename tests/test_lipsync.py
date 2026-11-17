import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.lipsync import (
    HeyGenLipsync,
    ReplicateHeyGenLipsync,
    SyncSoLipsync,
    make_lipsync_client,
)


def _settings(sync_key="sy-xxx", heygen_key="hg-xxx", replicate_token="rp-xxx"):
    return Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="g",
        elevenlabs_api_key="el", sync_api_key=sync_key, heygen_api_key=heygen_key,
        replicate_api_token=replicate_token,
    )


def _fake_client(get_sequence):
    """get_sequence: list of MagicMock generations returned by successive get()."""
    fake = MagicMock()
    fake.generations.create = AsyncMock(return_value=MagicMock(id="job-1"))
    fake.generations.get = AsyncMock(side_effect=get_sequence)
    return fake


def test_requires_api_key():
    with pytest.raises(RuntimeError, match="SYNC_API_KEY"):
        SyncSoLipsync(_settings(sync_key=""))


@pytest.mark.asyncio
async def test_lipsync_polls_to_completion():
    seq = [
        MagicMock(status="PROCESSING", output_url=None),
        MagicMock(status="PROCESSING", output_url=None),
        MagicMock(status="COMPLETED", output_url="https://out/clip.mp4",
                  output_duration=5.0),
    ]
    fake = _fake_client(seq)
    ls = SyncSoLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    result = await ls.lipsync(
        video_url="https://v/clip.mp4", audio_url="https://a/vo.wav",
    )

    assert result.output_url == "https://out/clip.mp4"
    assert result.output_duration == 5.0
    # create called with the right model + sync_mode only.
    ckw = fake.generations.create.call_args.kwargs
    assert ckw["model"] == "sync-3"
    assert ckw["options"].sync_mode == "silence"
    assert fake.generations.get.await_count == 3


@pytest.mark.asyncio
async def test_lipsync_sends_only_sync_mode_option():
    """sync-3 removed temperature/occlusion_detection_enabled (expressiveness
    and obstruction handling are native) — sending them is dead config. The
    GenerationOptions must carry sync_mode and nothing else."""
    seq = [MagicMock(status="COMPLETED", output_url="https://out/clip.mp4",
                     output_duration=5.0)]
    fake = _fake_client(seq)
    ls = SyncSoLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    await ls.lipsync(video_url="https://v/c.mp4", audio_url="https://a/v.wav")

    opts = fake.generations.create.call_args.kwargs["options"]
    assert opts.sync_mode == "silence"
    assert getattr(opts, "temperature", None) is None
    assert getattr(opts, "occlusion_detection_enabled", None) is None


@pytest.mark.asyncio
async def test_lipsync_raises_on_failed():
    seq = [MagicMock(status="FAILED", error="face not detected", error_code="E1")]
    fake = _fake_client(seq)
    ls = SyncSoLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    with pytest.raises(RuntimeError, match="face not detected"):
        await ls.lipsync(video_url="https://v/c.mp4", audio_url="https://a/v.wav")


@pytest.mark.asyncio
async def test_lipsync_raises_when_completed_without_url():
    seq = [MagicMock(status="COMPLETED", output_url=None)]
    fake = _fake_client(seq)
    ls = SyncSoLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    with pytest.raises(RuntimeError, match="no output_url"):
        await ls.lipsync(video_url="https://v/c.mp4", audio_url="https://a/v.wav")


# --- HeyGen ----------------------------------------------------------------

class _Resp:
    """Minimal stand-in for an httpx.Response."""
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _fake_heygen(post_payload, get_payloads):
    """post_payload: create response dict; get_payloads: poll response dicts."""
    fake = MagicMock()
    fake.post = AsyncMock(return_value=_Resp(post_payload))
    fake.get = AsyncMock(side_effect=[_Resp(p) for p in get_payloads])
    return fake


def test_heygen_requires_api_key():
    with pytest.raises(RuntimeError, match="HEYGEN_API_KEY"):
        HeyGenLipsync(_settings(heygen_key=""))


@pytest.mark.asyncio
async def test_heygen_polls_to_completion():
    create = {"data": {"lipsync_id": "ls-1"}}
    polls = [
        {"data": {"id": "ls-1", "status": "pending", "video_url": None}},
        {"data": {"id": "ls-1", "status": "running", "video_url": None}},
        {"data": {"id": "ls-1", "status": "completed",
                  "video_url": "https://out/clip.mp4", "duration": 5.0}},
    ]
    fake = _fake_heygen(create, polls)
    ls = HeyGenLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    result = await ls.lipsync(
        video_url="https://v/clip.mp4", audio_url="https://a/vo.wav",
        output_file_name="beat-3",
    )

    assert result.job_id == "ls-1"
    assert result.output_url == "https://out/clip.mp4"
    assert result.output_duration == 5.0
    # Create body: BYO url inputs + duration locked to the input clip.
    body = fake.post.call_args.kwargs["json"]
    assert body["video"] == {"type": "url", "url": "https://v/clip.mp4"}
    assert body["audio"] == {"type": "url", "url": "https://a/vo.wav"}
    assert body["mode"] == "precision"
    assert body["enable_dynamic_duration"] is False
    assert body["keep_the_same_format"] is True
    assert body["fps_mode"] == "passthrough"
    assert body["title"] == "beat-3"
    # Auth header + endpoints.
    assert fake.post.call_args.kwargs["headers"]["x-api-key"] == "hg-xxx"
    assert fake.post.call_args.args[0].endswith("/v3/lipsyncs")
    assert fake.get.call_args.args[0].endswith("/v3/lipsyncs/ls-1")
    assert fake.get.await_count == 3


@pytest.mark.asyncio
async def test_heygen_raises_on_failed():
    create = {"data": {"lipsync_id": "ls-2"}}
    polls = [{"data": {"id": "ls-2", "status": "failed",
                       "failure_message": "no face detected"}}]
    fake = _fake_heygen(create, polls)
    ls = HeyGenLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    with pytest.raises(RuntimeError, match="no face detected"):
        await ls.lipsync(video_url="https://v/c.mp4", audio_url="https://a/v.wav")


@pytest.mark.asyncio
async def test_heygen_raises_when_completed_without_url():
    create = {"data": {"lipsync_id": "ls-3"}}
    polls = [{"data": {"id": "ls-3", "status": "completed", "video_url": None}}]
    fake = _fake_heygen(create, polls)
    ls = HeyGenLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    with pytest.raises(RuntimeError, match="no video_url"):
        await ls.lipsync(video_url="https://v/c.mp4", audio_url="https://a/v.wav")


@pytest.mark.asyncio
async def test_heygen_raises_when_no_lipsync_id():
    fake = _fake_heygen({"data": {}}, [])
    ls = HeyGenLipsync(_settings(), poll_interval_seconds=0, _client=fake)

    with pytest.raises(RuntimeError, match="no lipsync_id"):
        await ls.lipsync(video_url="https://v/c.mp4", audio_url="https://a/v.wav")


def test_make_lipsync_client_dispatch():
    assert isinstance(make_lipsync_client(_settings(), "syncso"), SyncSoLipsync)
    assert isinstance(make_lipsync_client(_settings(), "heygen"), HeyGenLipsync)
    # seedance_native never builds a post-hoc client; falls back to sync.so.
    assert isinstance(
        make_lipsync_client(_settings(), "seedance_native"), SyncSoLipsync
    )


# ── ReplicateHeyGenLipsync (heygen/lipsync-precision via Replicate) ───────────

def test_replicate_heygen_requires_token():
    with pytest.raises(RuntimeError, match="REPLICATE_API_TOKEN"):
        ReplicateHeyGenLipsync(_settings(replicate_token=""))


def test_make_lipsync_client_routes_heygen_replicate():
    c = make_lipsync_client(_settings(), "heygen_replicate")
    assert isinstance(c, ReplicateHeyGenLipsync)
    # heygen/lipsync-precision is a dub model: it validates the INPUT video
    # carries an audio stream ("Your video's audio is missing or corrupted"),
    # so assembly must mux the beat audio onto the silent clip before upload.
    assert c.video_needs_audio is True
    # Sync.so takes silent video + separate audio — no mux flag at all.
    assert getattr(make_lipsync_client(_settings(), "syncso"),
                   "video_needs_audio", False) is False


@pytest.mark.asyncio
async def test_replicate_heygen_runs_model_and_returns_url():
    fake = MagicMock()
    fake.async_run = AsyncMock(return_value="https://replicate.delivery/out.mp4")
    c = ReplicateHeyGenLipsync(_settings(), model="heygen/lipsync-precision",
                               _client=fake)
    res = await c.lipsync(video_url="https://v", audio_url="https://a",
                          output_file_name="beat-0")
    assert res.output_url == "https://replicate.delivery/out.mp4"
    args, kwargs = fake.async_run.call_args
    assert args[0] == "heygen/lipsync-precision"
    inp = kwargs["input"]
    assert inp["video"] == "https://v" and inp["audio"] == "https://a"
    assert inp["dynamic_duration"] is False     # length lock for isolated audio


@pytest.mark.asyncio
async def test_replicate_heygen_retries_on_429_then_succeeds(monkeypatch):
    """Accounts under $5 credit are throttled (429, burst 1). A throttled
    submission must back off and retry, not fail the whole assembly."""
    class _Throttle(Exception):
        status = 429

    fake = MagicMock()
    fake.async_run = AsyncMock(side_effect=[
        _Throttle("Request was throttled"),
        _Throttle("Request was throttled"),
        "https://replicate.delivery/out.mp4",
    ])
    c = ReplicateHeyGenLipsync(_settings(), _client=fake)
    slept = []
    async def _fake_sleep(d): slept.append(d)
    monkeypatch.setattr("app.lipsync.asyncio.sleep", _fake_sleep)

    res = await c.lipsync(video_url="v", audio_url="a")
    assert res.output_url == "https://replicate.delivery/out.mp4"
    assert fake.async_run.call_count == 3   # 2 throttles + 1 success
    assert len(slept) == 2                   # backed off before each retry


@pytest.mark.asyncio
async def test_replicate_heygen_non_429_error_propagates(monkeypatch):
    """A non-throttle error must NOT be retried — it surfaces immediately."""
    fake = MagicMock()
    fake.async_run = AsyncMock(side_effect=ValueError("bad input"))
    c = ReplicateHeyGenLipsync(_settings(), _client=fake)
    monkeypatch.setattr("app.lipsync.asyncio.sleep", AsyncMock())
    with pytest.raises(ValueError, match="bad input"):
        await c.lipsync(video_url="v", audio_url="a")
    assert fake.async_run.call_count == 1   # no retry


@pytest.mark.asyncio
async def test_replicate_heygen_coerces_fileoutput_and_list(tmp_path):
    # FileOutput-like object (str() → url) wrapped in a 1-item list.
    class _FO:
        def __str__(self): return "https://replicate.delivery/f.mp4"
    fake = MagicMock()
    fake.async_run = AsyncMock(return_value=[_FO()])
    c = ReplicateHeyGenLipsync(_settings(), _client=fake)
    res = await c.lipsync(video_url="v", audio_url="a")
    assert res.output_url == "https://replicate.delivery/f.mp4"
