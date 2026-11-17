"""Parallel multi-chunk Seedance video runner (FLF2V).

Each chunk has BOTH a first_frame_url and last_frame_url pre-generated
upstream by the workflow handler. All chunks are independent — they only
depend on the pre-generated anchors, not on prior video chunks — so we
can submit them in parallel and poll concurrently via ``asyncio.gather``.

The single sequential alternative (run_chain_sequential) is kept for
callers that prefer fail-fast cost-bounding behavior; the workflow tab
uses the parallel run_chain by default.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from app.video_gen import (
    DEFAULT_MODEL_ID,
    EvoLinkSeedanceGenerator,
    VideoRequest,
    VideoResult,
)


@dataclass(frozen=True)
class ChunkPlan:
    """One chunk of an FLF2V chain: prompt + anchor pair + per-chunk settings."""
    seedance_prompt: str
    first_frame_url: str
    last_frame_url: str
    duration: int
    aspect_ratio: str
    resolution: str
    generate_audio: bool
    seed: int | None = None
    model: str = DEFAULT_MODEL_ID


@dataclass(frozen=True)
class ChunkResult:
    index: int
    task_id: str
    video_url: str
    last_frame_url: str | None   # EvoLink does not return last frame; always None
    raw: dict = field(default_factory=dict)


class ChainFailure(Exception):
    def __init__(self, *, chunk_index: int,
                 partial_results: list[ChunkResult],
                 underlying: BaseException):
        super().__init__(f"chain failed at chunk {chunk_index}: {underlying}")
        self.chunk_index = chunk_index
        self.partial_results = partial_results
        self.underlying = underlying


async def run_chain(
    *, client: EvoLinkSeedanceGenerator,
    chunks: list[ChunkPlan],
    on_progress: Callable[[int, str], None],
) -> list[ChunkResult]:
    """Submit all N chunks in parallel, poll concurrently, return results.

    Identity propagates via the anchor frames themselves (generated upstream
    in Nano Banana with a shared CharacterDescription). FLF2V pins both
    endpoints so chunks don't need to know about each other — they only
    need their two anchors. That independence is what makes parallel safe.

    If any chunk fails the gather short-circuits: cancellation propagates
    to in-flight chunks. The error is wrapped in ChainFailure with the
    failed chunk's index and whatever partial results were already
    completed. Cost-risk note: every submitted chunk bills regardless of
    whether the gather aborts, so a failure at chunk 3 of 4 may still
    bill for chunks 0/1/2/3 depending on EvoLink's billing semantics.
    """
    results: list[ChunkResult | None] = [None] * len(chunks)

    async def _run_one(i: int, plan: ChunkPlan) -> None:
        request = VideoRequest(
            prompt=plan.seedance_prompt,
            first_frame_url=plan.first_frame_url,
            last_frame_url=plan.last_frame_url,
            duration=plan.duration,
            aspect_ratio=plan.aspect_ratio,
            resolution=plan.resolution,
            generate_audio=plan.generate_audio,
            seed=plan.seed,
            model=plan.model,
        )
        on_progress(i, f"creating chunk {i + 1}/{len(chunks)} task (FLF2V)...")
        task_id, _payload = await client.create_task_async(request)
        on_progress(i, f"chunk {i + 1} task_id={task_id}; polling")

        final: VideoResult | None = None
        async for status, resp in client.poll_task_stream_async(task_id):
            on_progress(i, f"chunk {i + 1} status={status}")
            if resp is not None:
                final = resp

        if final is None:
            raise RuntimeError(f"chunk {i} poll loop exited without a final response")
        results[i] = ChunkResult(
            index=i, task_id=task_id,
            video_url=final.video_url,
            last_frame_url=None,
            raw=final.raw,
        )

    try:
        await asyncio.gather(*[_run_one(i, p) for i, p in enumerate(chunks)])
    except BaseException as e:
        failed_index = next(
            (i for i, r in enumerate(results) if r is None), 0,
        )
        partial = [r for r in results if r is not None]
        raise ChainFailure(
            chunk_index=failed_index, partial_results=partial, underlying=e,
        ) from e

    # All chunks succeeded — all entries are populated.
    return [r for r in results if r is not None]
