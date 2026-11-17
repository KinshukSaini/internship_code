import pytest
from unittest.mock import AsyncMock, MagicMock

from app.chain import ChainFailure, ChunkPlan, run_chain
from app.video_gen import VideoResult


def _plan(prompt="hi",
          first_frame_url="https://ff/a.png",
          last_frame_url="https://lf/a.png"):
    return ChunkPlan(
        seedance_prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        duration=8,
        aspect_ratio="9:16",
        resolution="720p",
        generate_audio=True,
        seed=None,
    )


def _fake_async_client_seq(responses: list[dict]):
    """Async-aware fake mirroring EvoLinkSeedanceGenerator's async API.

    Each response: {"task_id": str, "video_url": str}
    create_task_async returns (task_id, payload).
    poll_task_stream_async yields a single ("completed", VideoResult).
    """
    client = MagicMock()
    call_count = {"n": 0}

    async def fake_create(request):
        i = call_count["n"]; call_count["n"] += 1
        tid = responses[i]["task_id"]
        return tid, {"task_id": tid, "request": vars(request)}

    def fake_stream(task_id):
        # async generator: yields ("completed", VideoResult)
        async def _gen():
            for r in responses:
                if r["task_id"] == task_id:
                    yield "completed", VideoResult(
                        task_id=task_id, video_url=r["video_url"],
                        raw={"status": "completed", "results": [r["video_url"]]},
                    )
                    return
            raise AssertionError(f"unknown task_id {task_id}")
        return _gen()

    client.create_task_async = AsyncMock(side_effect=fake_create)
    client.poll_task_stream_async = MagicMock(side_effect=fake_stream)
    return client


@pytest.mark.asyncio
async def test_chain_runs_chunks_in_parallel():
    responses = [
        {"task_id": "t-0", "video_url": "https://v/0.mp4"},
        {"task_id": "t-1", "video_url": "https://v/1.mp4"},
    ]
    client = _fake_async_client_seq(responses)
    chunks = [
        _plan(prompt="A",
              first_frame_url="https://anchor/0.png",
              last_frame_url="https://anchor/1.png"),
        _plan(prompt="B",
              first_frame_url="https://anchor/1.png",
              last_frame_url="https://anchor/2.png"),
    ]

    results = await run_chain(
        client=client, chunks=chunks,
        on_progress=lambda i, m: None,
    )

    # Order preserved by index even though chunks may complete out of order.
    assert [r.video_url for r in results] == ["https://v/0.mp4", "https://v/1.mp4"]
    # Both chunks were submitted (parallel — gather fires all create_task_async
    # before any of them necessarily resolves).
    assert client.create_task_async.await_count == 2

    # Each chunk's request carries its own anchors.
    submitted = [c.args[0] for c in client.create_task_async.await_args_list]
    submitted_pairs = {
        (r.first_frame_url, r.last_frame_url) for r in submitted
    }
    assert submitted_pairs == {
        ("https://anchor/0.png", "https://anchor/1.png"),
        ("https://anchor/1.png", "https://anchor/2.png"),
    }


@pytest.mark.asyncio
async def test_chain_failure_wraps_in_chain_failure():
    call_count = {"n": 0}

    async def fake_create(request):
        i = call_count["n"]; call_count["n"] += 1
        return ["t-0", "t-1"][i], {"task_id": ["t-0", "t-1"][i]}

    def stream_with_failure(task_id):
        async def _gen():
            if task_id == "t-1":
                raise RuntimeError("task failed: simulated boom")
            yield "completed", VideoResult(
                task_id=task_id, video_url="v0",
                raw={"status": "completed", "results": ["v0"]},
            )
        return _gen()

    client = MagicMock()
    client.create_task_async = AsyncMock(side_effect=fake_create)
    client.poll_task_stream_async = MagicMock(side_effect=stream_with_failure)

    chunks = [_plan(), _plan()]
    with pytest.raises(ChainFailure) as exc:
        await run_chain(
            client=client, chunks=chunks,
            on_progress=lambda i, m: None,
        )
    # The failure propagates; one chunk may have completed by then.
    assert exc.value.underlying is not None
    assert "simulated boom" in str(exc.value.underlying)
