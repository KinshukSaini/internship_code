"""Sync.so lipsync — sync a talking-head clip's mouth to a separate VO track.

Used on SILENT Seedance clips (we lipsync against the ElevenLabs VO slice for
that beat). Model sync-3 is chosen because it handles still/barely-moving lips
common in AI-generated video (lipsync-2 fails on still frames).

Submit → async poll → return the output video URL. The output URL TTL is
undocumented, so callers should download immediately and re-host.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from app import costs
from app.config import (
    DEFAULT_LIPSYNC_ENGINE,
    HEYGEN_API_BASE,
    HEYGEN_LIPSYNC_MODE,
    HEYGEN_REPLICATE_MODEL,
    REPLICATE_TIMEOUT_S,
    SYNC_MODEL,
    Settings,
)

TERMINAL_OK = "COMPLETED"
TERMINAL_BAD = {"FAILED", "REJECTED"}

# HeyGen /v3/lipsyncs status enum (VideoTranslationStatus).
HEYGEN_OK = "completed"
HEYGEN_BAD = "failed"


def _bill(provider: str, model: str, result: "LipsyncResult",
          fallback_seconds: float | None) -> None:
    """Meter one lipsync pass — every engine here bills per output second.

    Prefer the duration the provider reports; fall back to the caller's known
    input length, which is exact for these engines because all three are
    configured to lock output length to the input (sync_mode="silence",
    enable_dynamic_duration=False, dynamic_duration=False).
    """
    seconds = result.output_duration
    if seconds is None:
        seconds = fallback_seconds
    if not seconds:
        costs.note_unpriced(f"lipsync:{provider}:duration-unknown")
        return
    costs.record_lipsync(provider=provider, model=model,
                         seconds=float(seconds), ref=result.job_id)


@dataclass(frozen=True)
class LipsyncResult:
    job_id: str
    output_url: str
    output_duration: float | None
    raw: dict


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class SyncSoLipsync:
    """sync-3 needs no generation tuning: temperature and
    occlusion_detection_enabled were removed from the model (expressiveness
    and obstruction handling are native) — sync_mode is the only option."""

    def __init__(self, settings: Settings, *,
                 poll_interval_seconds: float = 10.0,
                 _client: Any = None):
        if not settings.sync_api_key:
            raise RuntimeError(
                "SYNC_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if _client is not None:
            self._client = _client
        else:
            from sync import AsyncSync
            self._client = AsyncSync(api_key=settings.sync_api_key)
        self._poll = poll_interval_seconds

    async def lipsync(
        self, *, video_url: str, audio_url: str,
        model: str = SYNC_MODEL,
        sync_mode: str = "silence",
        output_file_name: str | None = None,
        billed_seconds: float | None = None,
    ) -> LipsyncResult:
        """Submit a lipsync job, poll to completion, return the output URL.

        sync_mode="silence": if the audio is shorter than the clip, the video
        plays full length and the mouth rests after speech (safest when clip
        and VO-slice lengths don't match exactly).
        """
        from sync import Audio, GenerationOptions, Video

        job = await self._client.generations.create(
            model=model,
            input=[Video(url=video_url), Audio(url=audio_url)],
            options=GenerationOptions(sync_mode=sync_mode),
            output_file_name=output_file_name,
        )
        job_id = _get(job, "id")
        if not job_id:
            raise RuntimeError(f"Sync.so create returned no job id: {job}")

        while True:
            gen = await self._client.generations.get(job_id)
            status = str(_get(gen, "status") or "")
            if status == TERMINAL_OK:
                url = _get(gen, "output_url")
                if not url:
                    raise RuntimeError(
                        f"Sync.so completed but no output_url: {gen}"
                    )
                res = LipsyncResult(
                    job_id=str(job_id), output_url=url,
                    output_duration=_get(gen, "output_duration"),
                    raw={"status": status, "output_url": url},
                )
                _bill("syncso", model, res, billed_seconds)
                return res
            if status in TERMINAL_BAD:
                err = _get(gen, "error") or _get(gen, "error_code") or status
                raise RuntimeError(f"Sync.so lipsync {status}: {err}")
            await asyncio.sleep(self._poll)


class HeyGenLipsync:
    """HeyGen post-hoc lipsync via POST /v3/lipsyncs.

    Bring-your-own video + audio: we hand HeyGen the already-uploaded clip URL +
    the VO-slice URL and it re-renders ONLY the mouth. Like Sync.so, we keep
    only the VIDEO from the result and discard its audio (the continuous
    ElevenLabs VO is overlaid globally), so the engine swap is transparent to
    the rest of assembly.

    Duration is locked to the input (enable_dynamic_duration=False,
    keep_the_same_format=True, fps_mode="passthrough") so the downstream
    window-trim + global VO overlay stay aligned — the same contract Sync.so's
    sync_mode="silence" gives us. Output video_url is presigned/expiring, so
    callers must download immediately (assembly already does).

    HeyGen REQUIRES the input video to carry an audio stream (it rejects
    audio-less Seedance i2v clips with "Your video's audio is missing or
    corrupted"). ``video_needs_audio`` tells assembly to mux the VO slice onto
    the silent clip before upload; Sync.so has no such requirement.
    """

    video_needs_audio = True

    def __init__(self, settings: Settings, *,
                 poll_interval_seconds: float = 10.0,
                 mode: str = HEYGEN_LIPSYNC_MODE, _client: Any = None):
        if not settings.heygen_api_key:
            raise RuntimeError(
                "HEYGEN_API_KEY is not set. Add it to .env (see .env.example)."
            )
        self._key = settings.heygen_api_key
        self._base = HEYGEN_API_BASE
        self._mode = mode
        self._poll = poll_interval_seconds
        if _client is not None:
            self._client = _client
        else:
            import httpx
            self._client = httpx.AsyncClient(timeout=60.0)

    @property
    def _headers(self) -> dict:
        return {"x-api-key": self._key}

    async def lipsync(
        self, *, video_url: str, audio_url: str,
        model: str = SYNC_MODEL,        # accepted for interface parity; unused
        sync_mode: str = "silence",     # accepted for interface parity; unused
        output_file_name: str | None = None,
        billed_seconds: float | None = None,
    ) -> LipsyncResult:
        body: dict[str, Any] = {
            "video": {"type": "url", "url": video_url},
            "audio": {"type": "url", "url": audio_url},
            "mode": self._mode,
            # Lock output to the input clip's length/encoding so the existing
            # window-trim invariant holds (no A/V drift).
            "enable_dynamic_duration": False,
            "keep_the_same_format": True,
            "fps_mode": "passthrough",
        }
        if output_file_name:
            body["title"] = output_file_name

        resp = await self._client.post(
            f"{self._base}/v3/lipsyncs", json=body, headers=self._headers,
        )
        resp.raise_for_status()
        data = _get(resp.json(), "data") or {}
        job_id = _get(data, "lipsync_id")
        if not job_id:
            raise RuntimeError(f"HeyGen create returned no lipsync_id: {data}")

        while True:
            resp = await self._client.get(
                f"{self._base}/v3/lipsyncs/{job_id}", headers=self._headers,
            )
            resp.raise_for_status()
            data = _get(resp.json(), "data") or {}
            status = str(_get(data, "status") or "")
            if status == HEYGEN_OK:
                url = _get(data, "video_url")
                if not url:
                    raise RuntimeError(
                        f"HeyGen completed but no video_url: {data}"
                    )
                res = LipsyncResult(
                    job_id=str(job_id), output_url=url,
                    output_duration=_get(data, "duration"),
                    raw={"status": status, "video_url": url},
                )
                # Direct HeyGen billing is credit-based on a HeyGen plan, not
                # a published per-second API rate like Replicate's resale of
                # the same engine — flagged rather than priced at a guess.
                costs.note_unpriced("lipsync:heygen-direct")
                return res
            if status == HEYGEN_BAD:
                err = _get(data, "failure_message") or status
                raise RuntimeError(f"HeyGen lipsync failed: {err}")
            await asyncio.sleep(self._poll)


class ReplicateHeyGenLipsync:
    """HeyGen lipsync hosted on Replicate (heygen/lipsync-precision).

    Same engine as HeyGen's direct API, billed through Replicate. The model
    takes the source clip + a separate audio track and re-renders ONLY the
    mouth — but like the direct API it is a dub model that validates the INPUT
    video carries an audio stream ("Your video's audio is missing or
    corrupted"), and Seedance i2v clips are silent. ``video_needs_audio =
    True`` tells assembly to mux the beat's padded isolated audio onto the
    clip before upload (the same audio is also the lipsync target).

    Used by the ``heygen_replicate`` engine, which feeds ONE isolated audio
    clip per beat (the full dialogue line, its own TTS) so there is no shared
    VO slicing and no cross-clip A/V drift. ``replicate.async_run`` blocks
    until the prediction completes and returns the output file, so there is no
    manual polling. The output URL is expiring — callers download immediately.
    """

    video_needs_audio = True

    def __init__(self, settings: Settings, *,
                 model: str = HEYGEN_REPLICATE_MODEL,
                 dynamic_duration: bool = False,
                 speech_enhancement: bool = True,
                 _client: Any = None):
        if not settings.replicate_api_token:
            raise RuntimeError(
                "REPLICATE_API_TOKEN is not set. Add it to .env "
                "(see .env.example)."
            )
        self._model = model
        self._dynamic_duration = dynamic_duration
        self._speech_enhancement = speech_enhancement
        if _client is not None:
            self._client = _client
        else:
            import replicate
            # Long timeout: async_run blocks on the prediction, and HeyGen
            # lipsync jobs run 30s-2min — the httpx default read timeout would
            # trip mid-job (httpx.ReadTimeout).
            self._client = replicate.Client(
                api_token=settings.replicate_api_token,
                timeout=REPLICATE_TIMEOUT_S)

    @staticmethod
    def _is_rate_limited(exc: BaseException) -> bool:
        """Replicate throttles prediction creation with HTTP 429 — hard for
        accounts under $5 credit (6/min, burst 1). Detect it structurally
        (ReplicateError.status) with a message fallback."""
        if getattr(exc, "status", None) == 429:
            return True
        msg = str(exc).lower()
        return "429" in msg or "throttled" in msg or "rate limit" in msg

    async def _run_with_retry(self, model_input: dict) -> Any:
        """Submit to Replicate, retrying 429 throttles with backoff.

        The throttle resets in ~10s, so a bounded exponential backoff (with
        jitter, so concurrent beats don't retry in lockstep) recovers instead
        of failing the whole assembly. Non-429 errors propagate immediately.
        """
        import random  # noqa: PLC0415 — lazy, only on the replicate path

        max_attempts = 6
        for attempt in range(max_attempts):
            try:
                return await self._client.async_run(self._model, input=model_input)
            except Exception as exc:  # noqa: BLE001 — re-raise unless it's a 429
                if not self._is_rate_limited(exc) or attempt == max_attempts - 1:
                    raise
                # Base ~10s (the stated reset), exponential, capped, + jitter.
                delay = min(60.0, 10.0 * (2 ** attempt)) + random.uniform(0, 3)
                print(
                    f"[lipsync] Replicate throttled (429); retry "
                    f"{attempt + 1}/{max_attempts - 1} in {delay:.0f}s",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(delay)

    async def lipsync(
        self, *, video_url: str, audio_url: str,
        model: str = SYNC_MODEL,        # accepted for interface parity; unused
        sync_mode: str = "silence",     # accepted for interface parity; unused
        output_file_name: str | None = None,
        billed_seconds: float | None = None,
    ) -> LipsyncResult:
        # heygen/lipsync-precision inputs: video, audio, dynamic_duration,
        # speech_enhancement. dynamic_duration=False keeps the output the
        # input clip's length (our clip is already sized to the audio).
        output = await self._run_with_retry({
            "video": video_url,
            "audio": audio_url,
            "dynamic_duration": self._dynamic_duration,
            "speech_enhancement": self._speech_enhancement,
        })
        # Replicate returns a FileOutput (str() → URL), a URL string, or a
        # 1-item list of either. Coerce to a plain URL.
        if isinstance(output, (list, tuple)):
            output = output[0] if output else None
        if output is None:
            raise RuntimeError(
                f"Replicate {self._model} returned no output")
        url = getattr(output, "url", None) or str(output)
        res = LipsyncResult(
            job_id=output_file_name or self._model, output_url=url,
            output_duration=None,
            raw={"model": self._model, "video_url": url},
        )
        # Replicate returns no duration, but dynamic_duration=False locks the
        # output to the input clip's length — which is what the caller passes.
        _bill("replicate", self._model, res, billed_seconds)
        return res


def make_lipsync_client(
    settings: Settings, engine: str = DEFAULT_LIPSYNC_ENGINE,
) -> SyncSoLipsync | HeyGenLipsync | ReplicateHeyGenLipsync:
    """Build the post-hoc lipsync client for the chosen engine.

    "seedance_native" never reaches here — that route is audio-driven and
    handled before assembly, so it needs no post-hoc client.
    """
    if engine == "heygen_replicate":
        return ReplicateHeyGenLipsync(settings)
    if engine == "heygen":
        return HeyGenLipsync(settings)
    return SyncSoLipsync(settings)
