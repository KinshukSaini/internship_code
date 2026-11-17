"""Anthropic Claude integration. Async-only.

ClaudeBackend is the text-LLM backend GeminiClient routes to when the
provider is "claude". One generate() seam: tier picks the model, a schema
returns a validated Pydantic instance via messages.parse, no schema returns
plain text via messages.create.
"""
from __future__ import annotations

import sys
from typing import Any

from app import costs
from app.config import (
    CLAUDE_FAST_MODEL,
    CLAUDE_LITE_MODEL,
    CLAUDE_PRO_MODEL,
    Settings,
)


class ClaudeBackend:
    def __init__(self, settings: Settings, *, _client: Any = None):
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if _client is not None:
            self._client = _client
        else:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def generate(self, *, tier: str, prompt: str, schema=None,
                       system_instruction: "str | list[str] | None" = None,
                       temperature: float = 0.4,
                       image_url: str | None = None) -> Any:
        """One call against the tier's Claude model.

        tier "pro" → Opus: adaptive thinking + effort high; temperature is
        REMOVED on Opus (400 if sent) so it is never forwarded. tier "fast"
        → Sonnet: caller temperature, effort medium (its default is high).
        tier "lite" → Haiku: caller temperature, no output_config (effort
        unsupported). With schema: messages.parse → validated instance.
        Without: messages.create → text string.

        system_instruction: a single string OR a list of blocks ordered
        stable-prefix → volatile-suffix. Each block gets its own
        cache_control breakpoint. Caching is a PREFIX match on exact bytes,
        so passing the shared skill-MD as the first block (byte-identical
        across every method that loads the same skill file) lets those calls
        share ONE cache entry — the first warms it, the rest read at ~0.1x.
        Concatenating skill-MD + per-call context into one block instead
        gives each caller a distinct prefix and defeats that sharing.

        image_url: when set (non-empty), the user turn becomes multimodal
        (image + text) via a `url` image source — used for anchor-grounded
        motion prompts. An empty string degrades to a text-only turn.
        """
        if image_url:
            content: Any = [
                {"type": "image",
                 "source": {"type": "url", "url": image_url}},
                {"type": "text", "text": prompt},
            ]
        else:
            content = prompt
        kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": content}],
        }
        if tier == "pro":
            # 32000, not 16000 (2026-08-01): on Opus max_tokens caps
            # thinking + response TOGETHER, and a full VideoTimeline JSON
            # under adaptive thinking at effort=high was hitting the 16k
            # ceiling mid-JSON (dev-reported truncations). Streaming below
            # makes the larger cap safe.
            kwargs.update(
                model=CLAUDE_PRO_MODEL, max_tokens=32000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
            )
        elif tier == "fast":
            kwargs.update(
                model=CLAUDE_FAST_MODEL, max_tokens=8192,
                temperature=temperature,
                output_config={"effort": "medium"},
            )
        elif tier == "lite":
            kwargs.update(
                model=CLAUDE_LITE_MODEL, max_tokens=1024,
                temperature=temperature,
            )
        else:
            raise ValueError(
                f"unknown tier {tier!r} — expected 'pro', 'fast' or 'lite'"
            )
        if system_instruction is not None:
            blocks = (
                [system_instruction] if isinstance(system_instruction, str)
                else list(system_instruction)
            )
            # One cache_control breakpoint per block (API cap is 4; callers
            # pass at most 2). Each block is an independent cacheable prefix:
            # block 0 is the shared skill-MD (byte-identical across methods →
            # one shared entry), later blocks are per-call context that is
            # still identical across a fan-out (e.g. the timeline overview is
            # the same for all beats), so caching them helps the batch too.
            kwargs["system"] = [
                {"type": "text", "text": b, "cache_control": {"type": "ephemeral"}}
                if i < 4 else {"type": "text", "text": b}
                for i, b in enumerate(blocks)
            ]
        # STREAM every call (2026-08-01). Non-streaming requests above ~16k
        # max_tokens trip the SDK's >10-minute guard (the dev-reported
        # "streaming if the request takes more than 10 mins" ValueError), and
        # long planning turns on idle HTTP connections get dropped by proxies.
        # messages.stream() accepts output_format directly — get_final_message()
        # returns a ParsedMessage whose parsed_output is the validated schema
        # instance, so the schema path keeps messages.parse() semantics
        # (schema sanitization + client-side validation) with streaming wire
        # behavior.
        if schema is not None:
            kwargs["output_format"] = schema
        async with self._client.messages.stream(**kwargs) as stream:
            resp = await stream.get_final_message()
        self._log_usage(kwargs["model"], resp)
        self._raise_on_refusal(resp)
        self._raise_on_truncation(resp, kwargs["max_tokens"])
        if schema is not None:
            if resp.parsed_output is None:
                raise ValueError(
                    "claude returned no parseable structured output "
                    f"(stop_reason={resp.stop_reason!r})"
                )
            return resp.parsed_output
        return next(b.text for b in resp.content if b.type == "text")

    @staticmethod
    def _log_usage(model: str, resp: Any) -> None:
        # Cache hits are only provable from usage (a marker below the model's
        # minimum cacheable prefix silently doesn't cache) — surface the
        # numbers so real runs show the read/write split per call.
        u = getattr(resp, "usage", None)
        if u is None:
            return
        # Same object the log line reads, so the run's LLM spend is metered
        # from what Anthropic reported rather than re-counted from the prompt.
        costs.record_claude(model, u)
        print(
            f"[claude] {model}: in={getattr(u, 'input_tokens', '?')} "
            f"cache_read={getattr(u, 'cache_read_input_tokens', 0) or 0} "
            f"cache_write={getattr(u, 'cache_creation_input_tokens', 0) or 0} "
            f"out={getattr(u, 'output_tokens', '?')}",
            file=sys.stderr, flush=True,
        )

    @staticmethod
    def _raise_on_truncation(resp: Any, max_tokens: int) -> None:
        """Reject a response that ran out of tokens instead of finishing.

        On Opus max_tokens caps thinking + response TOGETHER, so a long planning
        turn can burn the budget mid-JSON — which is exactly why the pro ceiling
        went 16k → 32k on 2026-08-01. Nothing was CHECKING for it though: a
        truncated structured response either surfaces as parsed_output=None (a
        confusing "no parseable output") or, worse, as a partially-filled object
        that looks valid. ValueError so _with_retry treats it as a bad sample.
        """
        if getattr(resp, "stop_reason", None) != "max_tokens":
            return
        out = getattr(getattr(resp, "usage", None), "output_tokens", "?")
        raise ValueError(
            f"claude hit the {max_tokens}-token ceiling before finishing "
            f"(output_tokens={out}) — thinking + response share this budget, so "
            f"the result is truncated and must not be trusted"
        )

    @staticmethod
    def _raise_on_refusal(resp: Any) -> None:
        # ValueError so _with_retry surfaces it like any other bad sample.
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise ValueError(f"claude refused the request: {details!r}")
