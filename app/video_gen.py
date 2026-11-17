"""EvoLink video generation client (Seedance 2.0 + Kling v3).

Replaces the previous BytePlus SDK wrapper. Targets the FLF2V
(image-to-video with first + last frame anchors) mode — the strongest
temporal stability mode Seedance 2.0 offers, as both endpoints are
pinned and the model interpolates the in-between frames.

Both model families ride the SAME EvoLink surface — POST /videos/generations,
Bearer auth, GET /tasks/{id} polling, identical task-id semantics — so only the
request BODY differs per family. That split lives in the `_BODY_BUILDERS`
dispatch; everything else (create/poll/retry/deadline) is shared.

Two parallel API surfaces:

  - Sync via ``urllib`` — used by the Gradio direct-video tab handler.
  - Async via ``httpx.AsyncClient`` — used by the workflow tab so
    multiple video chunks can run concurrently via ``asyncio.gather``.

Polling is exposed as a blocking call (sync) and a streaming generator
(both sync and async) so the UI can echo per-tick status updates.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterator, Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.config import (
    KLING_QUALITIES,
    OMNI_DUR_MAX,
    OMNI_DUR_MIN,
    OMNI_MAX_CONCURRENCY,
    Settings,
    VIDEO_GENERATORS,
)

EVOLINK_BASE = "https://api.evolink.ai/v1"

DEFAULT_MODEL_ID = "seedance-2.0-image-to-video"
FAST_MODEL_ID = "seedance-2.0-fast-image-to-video"

# Omni on EvoLink. Google splits Omni's modes with one request field
# (generation_config.video_config.task); EvoLink splits them into four model
# slugs on its ordinary video surface. Only i2v is wired (that is the only mode
# the pipeline orders), but the whole set is listed so provider_for_model
# recognises a slug someone pastes into the direct-video tab instead of
# mis-billing it as Seedance.
OMNI_EVOLINK_MODELS = (
    "gemini-omni-flash-image-to-video",
    "gemini-omni-flash-text-to-video",
    "gemini-omni-flash-reference-to-video",
    "gemini-omni-flash-video-edit",
)

# Per-poll retry budget for transient network failures. A 10-clip parallel
# gather polling every few seconds for ~60-120s = thousands of individual
# requests; transient ReadError / ReadTimeout / 5xx is statistically certain.
# 6 retries with exponential backoff buys ~minute of resilience per clip.
_MAX_POLL_RETRIES = 6

VideoStatus = Literal["processing", "completed", "failed", "queued", "running"]


@dataclass(frozen=True)
class VideoRequest:
    """Flat parameters for a single EvoLink FLF2V request.

    Provider-agnostic: fields a family does not support are simply dropped by
    that family's body builder (Kling has no seed/camera_fixed/aspect_ratio;
    Seedance has no negative_prompt).
    """
    prompt: str
    first_frame_url: str
    last_frame_url: str | None
    duration: int
    aspect_ratio: str
    resolution: str
    generate_audio: bool
    seed: int | None
    model: str = DEFAULT_MODEL_ID
    camera_fixed: bool = False
    audio_url: str | None = None
    # Kling-only. Seedance has no negative_prompt field, which is why our
    # Seedance prompts carry affirmative ban clauses instead (see the comment
    # in pipeline.py's b-roll prompt builder); on Kling those bans move here.
    negative_prompt: str | None = None


@dataclass(frozen=True)
class VideoResult:
    task_id: str
    video_url: str
    raw: dict


# ─── sync default transport (urllib) ─────────────────────────────────

def _default_post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise RuntimeError(
            f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
        ) from None


def _default_get(url: str, headers: dict, timeout: float) -> dict:
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise RuntimeError(
            f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
        ) from None


# ─── provider body builders ──────────────────────────────────────────
# Same endpoint, same auth, same polling — only the body shape differs, so the
# family is inferred from the model slug rather than carried as a second field
# that could contradict the model.

def provider_for_model(model: str) -> str:
    """Map an EvoLink model slug to its provider family.

    Fails fast on an unknown family: a body built with the wrong schema is a
    silently-rejected PAID request, so guessing is worse than raising.
    """
    if model.startswith("kling"):
        return "kling"
    if model.startswith("seedance"):
        return "seedance"
    if model in OMNI_EVOLINK_MODELS:
        return "omni_evolink"
    raise ValueError(f"unknown EvoLink model family: {model!r}")


def _kling_quality(resolution: str) -> str:
    """Translate our clip resolution into Kling's `quality` enum.

    Kling i2v has no 480p tier (720p is the floor), so a 480p run clamps UP —
    720p on Kling is still cheaper per second than 480p on Seedance.
    """
    if resolution == "480p":
        return "720p"
    if resolution in KLING_QUALITIES:
        return resolution
    raise ValueError(
        f"resolution {resolution!r} is not a Kling quality "
        f"(expected one of {KLING_QUALITIES} or 480p)"
    )


def _seedance_body(request: VideoRequest) -> dict:
    image_urls = [request.first_frame_url]
    if request.last_frame_url:
        image_urls.append(request.last_frame_url)
    body: dict = {
        "model": request.model,
        "prompt": request.prompt,
        "image_urls": image_urls,
        "duration": request.duration,
        "quality": request.resolution,
        "aspect_ratio": request.aspect_ratio,
        "generate_audio": request.generate_audio,
    }
    if request.camera_fixed:
        # False is the documented API default, so the key is only sent
        # when locking the camera — models we haven't verified (e.g.
        # reference-to-video) must never receive unknown keys.
        body["camera_fixed"] = True
    if request.audio_url:
        # EvoLink passes audio_urls through to ByteDance, which uses
        # them as reference audio for the generated clip.
        body["audio_urls"] = [request.audio_url]
    if request.seed is not None:
        body["seed"] = request.seed
    return body


def _kling_body(request: VideoRequest) -> dict:
    """Body for kling-v3-image-to-video.

    Deliberately NARROWER than the Seedance body — Kling's i2v schema has no
    aspect_ratio (it is derived from image_start), no seed (generation is
    non-reproducible), no camera_fixed (camera lock is done by omitting camera
    language from the prompt and banning it in negative_prompt) and no
    cfg_scale. Emitting any of them risks a 400 on a paid call.
    """
    if request.audio_url:
        # Programming error, not a provider hiccup: Kling has no audio-driven
        # generation, so audio-anchored beats (seedance_native background_vo,
        # native talking heads) must never be routed here.
        raise ValueError(
            "Kling has no audio-driven generation — a request with audio_url "
            "must stay on Seedance (reference-to-video)"
        )
    body: dict = {
        "model": request.model,
        "image_start": request.first_frame_url,
        "prompt": request.prompt,
        # Kling's duration is an INTEGER number of seconds, range 3-15.
        "duration": int(request.duration),
        "quality": _kling_quality(request.resolution),
        # `sound` is a STRING enum on Kling ("on"/"off"), not a boolean — we
        # never want provider-generated audio (VO + lipsync own the audio).
        "sound": "off",
    }
    if request.last_frame_url:
        body["image_end"] = request.last_frame_url
    if request.negative_prompt:
        body["negative_prompt"] = request.negative_prompt
    return body


def _omni_evolink_body(request: VideoRequest) -> dict:
    """Body for gemini-omni-flash-image-to-video on EvoLink.

    The NARROWEST body in this module, and deliberately so: EvoLink documents
    exactly five fields for the Omni slugs (model, prompt, image_urls,
    aspect_ratio, duration) and Omni has no analogue for the rest. There is no
    `quality`/resolution (Omni is 720p, full stop), no `generate_audio` (native
    speech is unconditional — it is the whole point of the model), no `seed`
    (EvoLink's Omni surface exposes none, which is why this route is the
    FALLBACK and not the default: cross-clip identity gets weaker without one)
    and no negative_prompt. Sending any of them risks a 400 on a paid call, so
    they are dropped here rather than passed through hopefully.

    Unlike Google's route this takes image URLs, not base64 — the anchor is
    already on R2, so the fallback needs no re-encode and no download.
    """
    if request.audio_url:
        # Same contract as _kling_body: an audio-anchored beat routed here would
        # silently lose its reference audio on a billed call. Omni generates its
        # own speech and accepts no audio input on EITHER route.
        raise ValueError(
            "Omni has no audio input on the EvoLink route either — an "
            "audio-anchored request must stay on Seedance"
        )
    if request.last_frame_url:
        raise ValueError(
            "Omni has no last-frame control — drop the last_frame_url or "
            "route to Seedance"
        )
    if request.aspect_ratio not in ("9:16", "16:9", "auto"):
        raise ValueError(
            f"EvoLink Omni supports 9:16, 16:9 and auto, got "
            f"{request.aspect_ratio!r}"
        )
    return {
        "model": request.model,
        "prompt": request.prompt,
        "image_urls": [request.first_frame_url],
        "aspect_ratio": request.aspect_ratio,
        # Integer seconds, 3-10 — the same range Google's route enforces, but
        # here it is a plain int rather than the "10s" string whose format
        # Google never published.
        "duration": max(OMNI_DUR_MIN, min(OMNI_DUR_MAX, int(request.duration))),
    }


_BODY_BUILDERS: dict[str, Callable[[VideoRequest], dict]] = {
    "seedance": _seedance_body,
    "kling": _kling_body,
    "omni_evolink": _omni_evolink_body,
}


class EvoLinkVideoClient:
    def __init__(
        self, settings: Settings, *,
        poll_interval_seconds: float = 10.0,
        _post: Callable[[str, dict, dict, float], dict] = _default_post,
        _get: Callable[[str, dict, float], dict] = _default_get,
        _async_client: "httpx.AsyncClient | None" = None,  # type: ignore[name-defined]
    ):
        if not settings.evolink_api_key:
            raise RuntimeError(
                "EVOLINK_API_KEY is not set. Add it to .env (see .env.example)."
            )
        self._api_key = settings.evolink_api_key
        self._poll_interval = poll_interval_seconds
        self._post = _post
        self._get = _get
        # Lazy-init the async HTTP client only if needed; tests can inject.
        self._async_client = _async_client

    # ─── sync API ────────────────────────────────────────────────────

    def create_task(self, request: VideoRequest) -> tuple[str, dict]:
        body = self._build_body(request)
        resp = self._post(
            f"{EVOLINK_BASE}/videos/generations",
            body, self._headers(), 180.0,
        )
        task_id = resp.get("id") or resp.get("task_id")
        if not task_id:
            raise RuntimeError(f"EvoLink response missing task id: {resp}")
        payload = {"task_id": task_id, "request": body, "create_response": resp}
        return task_id, payload

    def poll_task(self, task_id: str, *,
                  on_status: Callable[[str], None]) -> VideoResult:
        for status, result in self.poll_task_stream(task_id):
            on_status(status)
            if result is not None:
                return result
        raise RuntimeError("poll_task_stream ended without a result")

    def poll_task_stream(self, task_id: str) -> Iterator[tuple[str, "VideoResult | None"]]:
        while True:
            resp = self._get(
                f"{EVOLINK_BASE}/tasks/{task_id}", self._headers(), 60.0,
            )
            status = resp.get("status", "") or "processing"
            if status == "completed":
                url = self._extract_video_url(resp)
                yield "completed", VideoResult(
                    task_id=task_id, video_url=url, raw=resp,
                )
                return
            if status == "failed":
                err = resp.get("error") or resp.get("message") or "unknown error"
                raise RuntimeError(f"task failed: {err}")
            yield status, None
            time.sleep(self._poll_interval)

    # ─── async API ───────────────────────────────────────────────────

    async def create_task_async(self, request: VideoRequest) -> tuple[str, dict]:
        import httpx
        body = self._build_body(request)
        client = await self._get_async_client()
        last_err: Exception | None = None
        for attempt in range(1, _MAX_POLL_RETRIES + 1):
            try:
                resp = await client.post(
                    f"{EVOLINK_BASE}/videos/generations",
                    json=body, headers=self._headers(), timeout=180.0,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                last_err = e
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600:
                    last_err = e
                else:
                    raise RuntimeError(
                        f"EvoLink create_task failed: HTTP "
                        f"{e.response.status_code}: {e.response.text[:200]}"
                    ) from e
            if attempt < _MAX_POLL_RETRIES:
                wait = min(2.0 ** (attempt - 1), 30.0)
                print(
                    f"[evolink create retry {attempt}/{_MAX_POLL_RETRIES} "
                    f"in {wait:.1f}s] {type(last_err).__name__}: {last_err}",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(wait)
            else:
                raise RuntimeError(
                    f"EvoLink create_task failed after {attempt} retries: "
                    f"{type(last_err).__name__}: {last_err}"
                ) from last_err
        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            raise RuntimeError(f"EvoLink response missing task id: {data}")
        payload = {"task_id": task_id, "request": body, "create_response": data}
        return task_id, payload

    async def poll_task_stream_async(
        self, task_id: str, *, max_wait_seconds: float = 1200.0,
    ) -> AsyncIterator[tuple[str, "VideoResult | None"]]:
        """Poll an EvoLink task until terminal, yielding (status, result|None).

        Transient httpx network errors (ReadError, ReadTimeout, ConnectError,
        RemoteProtocolError) and EvoLink 5xx responses are RETRIED with
        exponential backoff up to _MAX_POLL_RETRIES — a blip on one poll
        must NOT kill the whole gather and discard 9 successful clips. Only
        non-transient HTTP errors (4xx auth / not-found) and an explicit
        task `status == "failed"` from EvoLink terminate the poll.

        ``max_wait_seconds`` bounds the total poll: a task stuck in
        processing/queued forever (provider wedge) raises instead of looping
        indefinitely and hanging the whole gather.
        """
        import httpx
        client = await self._get_async_client()
        consecutive_errors = 0
        start = time.monotonic()
        while True:
            if time.monotonic() - start >= max_wait_seconds:
                raise RuntimeError(
                    f"EvoLink task {task_id} exceeded the {max_wait_seconds:.0f}s "
                    f"poll deadline without completing"
                )
            try:
                resp = await client.get(
                    f"{EVOLINK_BASE}/tasks/{task_id}",
                    headers=self._headers(), timeout=60.0,
                )
                resp.raise_for_status()
                data = resp.json()
                consecutive_errors = 0
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                # Transient network blip — retry with backoff.
                consecutive_errors += 1
                if consecutive_errors >= _MAX_POLL_RETRIES:
                    raise RuntimeError(
                        f"EvoLink poll failed after {consecutive_errors} "
                        f"network retries: {type(e).__name__}: {e}"
                    ) from e
                wait = min(self._poll_interval * (2 ** (consecutive_errors - 1)), 30.0)
                print(
                    f"[evolink poll retry {consecutive_errors}/{_MAX_POLL_RETRIES} "
                    f"in {wait:.1f}s] {type(e).__name__}: {e}",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(wait)
                continue
            except httpx.HTTPStatusError as e:
                # 5xx is transient (EvoLink CDN hiccup); 4xx is fatal.
                if 500 <= e.response.status_code < 600:
                    consecutive_errors += 1
                    if consecutive_errors >= _MAX_POLL_RETRIES:
                        raise RuntimeError(
                            f"EvoLink poll failed after {consecutive_errors} "
                            f"5xx retries: {e.response.status_code}"
                        ) from e
                    wait = min(self._poll_interval * (2 ** (consecutive_errors - 1)), 30.0)
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(
                    f"EvoLink poll failed: HTTP {e.response.status_code}: "
                    f"{e.response.text[:200]}"
                ) from e
            except Exception as e:
                raise RuntimeError(
                    f"EvoLink poll failed: {type(e).__name__}: {e}"
                ) from e
            status = data.get("status", "") or "processing"
            if status == "completed":
                url = self._extract_video_url(data)
                yield "completed", VideoResult(
                    task_id=task_id, video_url=url, raw=data,
                )
                return
            if status == "failed":
                err = data.get("error") or data.get("message") or "unknown error"
                raise RuntimeError(f"task failed: {err}")
            yield status, None
            await asyncio.sleep(self._poll_interval)

    async def close(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    async def _get_async_client(self):
        if self._async_client is None:
            import httpx
            self._async_client = httpx.AsyncClient()
        return self._async_client

    # ─── shared helpers ─────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    def _build_body(self, request: VideoRequest) -> dict:
        return _BODY_BUILDERS[provider_for_model(request.model)](request)

    @staticmethod
    def _extract_video_url(resp: dict) -> str:
        results = resp.get("results") or []
        if not results:
            raise RuntimeError(f"EvoLink completed task has no results: {resp}")
        first = results[0]
        if isinstance(first, str):
            return first
        url = first.get("url") or first.get("video_url")
        if not url:
            raise RuntimeError(
                f"EvoLink result object has no url field: {first}"
            )
        return url


# ─── Omni (Google gemini-omni-flash-preview, Interactions API) ────────
# A different API family entirely: no EvoLink task/poll surface. The SDK call
# is synchronous, so both async methods run it through asyncio.to_thread. The
# generated video is fetched as BYTES (client.files.download — the documented
# files.get polling loop is broken, see docs/gemini-omni-research.md §2) and
# re-uploaded to R2 so VideoResult.video_url stays a durable, plainly
# downloadable URL like every other provider's (bonus: R2 doesn't expire the
# way EvoLink's CDN does at ~72h, so resume tier 2 stays alive longer).


# The interaction status vocabulary is NOT documented (research §3 verified the
# request surface, not this enum), and google-genai returns CaseInSensitiveEnum
# members whose str() is "ClassName.MEMBER". Normalizing to the bare, lower-cased
# member name — plus the obvious synonyms — is what stops a finished, fully-billed
# clip from spinning until the 20-minute poll deadline because the terminal state
# arrived as "InteractionStatus.COMPLETED" or "SUCCEEDED".
_OMNI_STATUS_ALIASES: dict[str, str] = {
    "succeeded": "completed", "success": "completed", "done": "completed",
    "error": "failed", "canceled": "cancelled",
}


def _omni_status(raw: Any) -> str:
    """Normalized terminal-state name for an interaction's status field."""
    value = getattr(raw, "value", raw)
    name = str(value or "in_progress").rsplit(".", 1)[-1].strip().lower()
    return _OMNI_STATUS_ALIASES.get(name, name)


def _omni_interaction_kwargs(request: VideoRequest, image_b64: str,
                             image_mime: str) -> dict:
    """Build the interactions.create kwargs for one clip.

    Module-level (like _seedance_body/_kling_body) so tests can assert the
    exact request shape without a client. Raises on inputs Omni cannot honor
    rather than silently dropping paid intent (the _kling_body precedent).
    """
    if request.audio_url:
        raise ValueError(
            "Omni has no audio input (audio references are unsupported) — "
            "an audio-anchored request must stay on Seedance"
        )
    if request.last_frame_url:
        raise ValueError(
            "Omni has no last-frame control (frame interpolation is "
            "unsupported) — drop the last_frame_url or route to Seedance"
        )
    if request.aspect_ratio not in ("9:16", "16:9"):
        raise ValueError(
            f"Omni supports only 9:16 and 16:9, got {request.aspect_ratio!r}"
        )
    duration = max(OMNI_DUR_MIN, min(OMNI_DUR_MAX, int(request.duration)))
    kwargs: dict = {
        "model": request.model,
        "input": [
            {"type": "image", "data": image_b64, "mime_type": image_mime},
            {"type": "text", "text": request.prompt},
        ],
        "generation_config": {"video_config": {"task": "image_to_video"}},
        "response_format": {
            "type": "video",
            "aspect_ratio": request.aspect_ratio,
            # Unverified value format (free-form string in the schema, no enum
            # published). Sent best-effort; create() drops it on a 400 and the
            # clip comes back 10s — assembly's speech-end trim recovers pacing.
            "duration": f"{duration}s",
            # Clips are >4MB; inline delivery hits payload limits.
            "delivery": "uri",
        },
        # Return the interaction id immediately so it can be persisted BEFORE
        # polling — same crash-recovery contract as EvoLink task ids.
        "background": True,
    }
    if request.seed is not None:
        kwargs["generation_config"]["seed"] = request.seed
    # resolution / camera_fixed / negative_prompt have no Omni analogue and are
    # merely absent (720p fixed; camera lock is prompt language) — dropped.
    return kwargs


class OmniVideoClient:
    """gemini-omni-flash-preview client with the pipeline's video-client surface.

    Duck-types EvoLinkVideoClient's async trio (create_task_async /
    poll_task_stream_async / close); the sync trio is deliberately absent — the
    direct-video tab stays on Seedance.
    """

    provider = "omni"

    def __init__(
        self, settings: Settings, *,
        poll_interval_seconds: float = 5.0,
        _client=None,
        _uploader=None,
        _fetch_image=None,
    ):
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if _client is not None:
            self._client = _client
        else:
            # Lazy on purpose (project convention): the SDK import is slow and
            # only needed when an Omni run actually starts.
            from google import genai
            self._client = genai.Client(api_key=settings.gemini_api_key)
        if _uploader is not None:
            self._uploader = _uploader
        else:
            from app.uploader import make_uploader
            self._uploader = make_uploader(settings)
        if _fetch_image is not None:
            self._fetch_image = _fetch_image
        else:
            # Reuse the cached, locked reference fetcher — anchors are fetched
            # once per URL even under a parallel clip fan-out.
            from app.image_gen import _fetch_reference_with_retry
            self._fetch_image = _fetch_reference_with_retry
        self._poll_interval = poll_interval_seconds
        # Gate CREATES only. The clip stage gathers every beat at once, so a
        # 24-beat run otherwise fires 24 creates in the same instant at a surface
        # whose documented sibling limit is ~10 req/min. Polls stay ungated: a
        # poll that cannot run is a clip nobody is watching, while its 20-minute
        # deadline keeps ticking.
        self._create_sem = asyncio.Semaphore(max(1, OMNI_MAX_CONCURRENCY))
        # Flipped False the first time the API rejects `duration` (see below).
        # Sticky for the client's lifetime so the rest of a run's clip fan-out
        # skips the create that is already known to fail — and so the caller can
        # bill those clips at the 10s default it silently fell back to.
        self.duration_supported = True

    # ─── async API (the only surface the v2 pipeline uses) ──────────

    async def create_task_async(self, request: VideoRequest) -> tuple[str, dict]:
        async with self._create_sem:
            return await self._create(request)

    async def _create(self, request: VideoRequest) -> tuple[str, dict]:
        import base64
        img_bytes, mime = await self._fetch_image(request.first_frame_url)
        image_b64 = base64.b64encode(img_bytes).decode()
        kwargs = _omni_interaction_kwargs(request, image_b64, mime)
        if not self.duration_supported:
            kwargs["response_format"].pop("duration", None)
        try:
            interaction = await asyncio.to_thread(
                self._client.interactions.create, **kwargs
            )
        except Exception as e:
            # The duration string format is the one unverified field in the
            # request. If the API rejects specifically it, retry once without —
            # a 10s clip with speech-end trim beats a dead run.
            if "duration" in str(e).lower() and _is_client_error(e):
                print(
                    "[omni] duration rejected by the API — retrying without "
                    f"(every clip in this run bills at the {OMNI_DUR_MAX}s "
                    f"default): {e}",
                    file=sys.stderr, flush=True,
                )
                self.duration_supported = False
                kwargs["response_format"].pop("duration", None)
                interaction = await asyncio.to_thread(
                    self._client.interactions.create, **kwargs
                )
            else:
                raise
        task_id = getattr(interaction, "id", None)
        if not task_id:
            raise RuntimeError(f"Omni interaction has no id: {interaction!r}")
        # Slim summary only — never the base64 image (megabytes per manifest
        # write × clips × updates).
        sent = {k: v for k, v in kwargs.items() if k != "input"}
        sent["prompt"] = request.prompt
        sent["first_frame_url"] = request.first_frame_url
        payload = {
            "task_id": task_id,
            "request": sent,
            "create_response": {"id": task_id,
                                "status": str(getattr(interaction, "status", ""))},
        }
        return task_id, payload

    async def poll_task_stream_async(
        self, task_id: str, *, max_wait_seconds: float = 1200.0,
    ) -> AsyncIterator[tuple[str, "VideoResult | None"]]:
        consecutive_errors = 0
        start = time.monotonic()
        while True:
            if time.monotonic() - start >= max_wait_seconds:
                raise RuntimeError(
                    f"Omni interaction {task_id} exceeded the "
                    f"{max_wait_seconds:.0f}s poll deadline without completing"
                )
            try:
                interaction = await asyncio.to_thread(
                    self._client.interactions.get, task_id
                )
                consecutive_errors = 0
            except Exception as e:
                if _is_client_error(e):
                    # The interaction id is the whole diagnosis: the id is what
                    # a later free GET can be pointed at (scratch-probe/
                    # omni_interaction_probe.py) to tell a PERMANENT rejection
                    # from one that has since resolved. Without it, a 400 here
                    # is unattributable — the 2026-08-05 incident lost three
                    # clips and left no id to interrogate.
                    raise RuntimeError(
                        f"Omni poll failed (non-retryable) for interaction "
                        f"{task_id}: {type(e).__name__}: {e}"
                    ) from e
                consecutive_errors += 1
                if consecutive_errors >= _MAX_POLL_RETRIES:
                    raise RuntimeError(
                        f"Omni poll failed after {consecutive_errors} "
                        f"retries: {type(e).__name__}: {e}"
                    ) from e
                wait = min(self._poll_interval * (2 ** (consecutive_errors - 1)), 30.0)
                print(
                    f"[omni poll retry {consecutive_errors}/{_MAX_POLL_RETRIES} "
                    f"in {wait:.1f}s] {type(e).__name__}: {e}",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(wait)
                continue
            status = _omni_status(getattr(interaction, "status", ""))
            if status == "completed":
                url = await self._deliver(task_id, interaction)
                yield "completed", VideoResult(
                    task_id=task_id, video_url=url,
                    raw={"id": task_id, "status": status},
                )
                return
            if status in ("failed", "cancelled", "expired"):
                raise RuntimeError(f"task failed: omni interaction {status}")
            # queued / in_progress / anything unknown → keep polling.
            yield "processing", None
            await asyncio.sleep(self._poll_interval)

    async def probe_status_async(self, task_id: str) -> tuple[str, bool]:
        """(status, artifact_present) for ONE interaction — nothing transferred.

        Resume's pre-commit preview only asks "is this clip recoverable for
        free?", and for Omni the answer is in the interaction record. Going
        through the poller instead would materialize the clip (a full Files-API
        download plus a fresh R2 upload) just to learn its status, because the
        poller delivers before it can yield "completed". The freshness check is
        weaker than EvoLink's range-GET (the Files URI needs the API key, so it
        cannot be probed with a plain HTTP GET): if the file has since expired,
        the commit's download fails and that beat falls back to a paid re-order —
        preview optimism, never a silent charge.
        """
        interaction = await asyncio.to_thread(
            self._client.interactions.get, task_id
        )
        uri = getattr(getattr(interaction, "output_video", None), "uri", None)
        return _omni_status(getattr(interaction, "status", "")), bool(uri)

    async def _deliver(self, task_id: str, interaction) -> str:
        """Download the finished clip's bytes and re-home them on R2."""
        import tempfile
        video = getattr(interaction, "output_video", None)
        uri = getattr(video, "uri", None)
        if not uri:
            raise RuntimeError(
                f"Omni interaction {task_id} completed without an output video"
            )
        # output_video.uri is already a direct :download?alt=media endpoint —
        # files.download() is the working path (the docs' files.get polling
        # loop dies with a JSONDecodeError; verified 2026-07-29).
        video_bytes = await asyncio.to_thread(
            self._client.files.download, file=uri
        )
        def _upload() -> str:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(video_bytes)
                tmp = f.name
            try:
                return self._uploader.upload(tmp)
            finally:
                import os
                os.unlink(tmp)
        return await asyncio.to_thread(_upload)

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                await asyncio.to_thread(close)
            except Exception:
                pass  # best-effort — nothing paid rides on client teardown


def _is_client_error(e: Exception) -> bool:
    """True when the SDK error is a 4xx (caller bug / bad field) — fatal.

    Uses the google.genai errors hierarchy when available, falling back to a
    status-code sniff so injected test fakes don't need the real SDK types.
    """
    try:
        from google.genai import errors
        if isinstance(e, errors.ClientError):
            return True
        if isinstance(e, errors.ServerError):
            return False
    except ImportError:
        pass
    code = getattr(e, "status_code", None) or getattr(e, "code", None)
    return isinstance(code, int) and 400 <= code < 500


# Pre-Kling name. Kept as an alias so existing imports (app/chain.py, tests)
# keep working without a mechanical rename across the repo.
EvoLinkSeedanceGenerator = EvoLinkVideoClient


def make_video_client(settings: Settings, video_generator: str = "seedance"):
    """Video-client factory, routed by the run's video_generator axis.

    "seedance" covers the whole EvoLink surface (Seedance AND per-beat Kling
    b-roll — same client, bodies split per model slug). "omni" is a different
    API family. Unknown values raise — the voice factory falls back to
    ElevenLabs on unknown names, but a wrong VIDEO client means every paid
    clip is generated by the wrong engine, so failing fast is the only safe
    behaviour (same rationale as provider_for_model).
    """
    if video_generator == "omni":
        return OmniVideoClient(settings)
    if video_generator == "seedance":
        return EvoLinkVideoClient(settings)
    raise ValueError(
        f"unknown video_generator {video_generator!r} "
        f"(expected one of {VIDEO_GENERATORS})"
    )
