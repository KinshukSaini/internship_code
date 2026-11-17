"""Exact per-run cost accounting.

Every number here comes from what a provider REPORTED, never from what the
pipeline planned. That distinction is the whole point: ``_clip_cost_estimate``
in pipeline.py prices the duration we *asked* for and is a pre-flight quote;
this module prices the tokens/characters/seconds/credits a provider says it
actually charged, after the fact.

Two grades of exactness coexist, and entries are tagged so they stay
distinguishable:

  * ``authoritative=True`` — the provider handed back a dollar figure. Only
    EvoLink does this (``usage.cost.usd`` on a completed task). Nothing we
    maintain can make it drift.
  * ``authoritative=False`` — real metered usage (tokens, characters, seconds)
    multiplied by a rate in RATES. The meter is exact; only the tariff can go
    stale, which is why every rate carries its source and check date.

A call we cannot price at all is recorded as an ``unpriced`` note rather than
silently dropped — a missing component must read as a gap, not as a cheap run.

Wiring: the active ledger lives in a ContextVar, so clients call the
module-level ``record_*`` helpers without threading a ledger through every
constructor. asyncio tasks inherit the context at creation time, so everything
spawned inside ``activate()`` bills to the right run. Outside a run the helpers
are no-ops, which keeps one-off scripts and the image-playground tab working
unchanged.
"""
from __future__ import annotations

import contextlib
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

# Bump when any rate below changes. Persisted with every run so an old
# manifest's numbers stay interpretable after a price change.
RATE_TABLE_VERSION = "2026-08-03"

# ── Rates ────────────────────────────────────────────────────────────────
# All USD. Sources checked 2026-08-03.

# platform.claude.com/docs/en/pricing — USD per 1M tokens (input, output).
# Cache multipliers apply to the INPUT rate: ephemeral 5-minute writes cost
# 1.25x and reads 0.1x. We only ever set the default 5m TTL (claude.py), so the
# 2x one-hour write rate is deliberately absent.
CLAUDE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10

# ai.google.dev/gemini-api/docs/pricing — USD per 1M tokens (input, output).
# gemini-3.1-pro bills a higher tier above a 200k-token prompt; the second
# tuple is that tier. Models absent here (the 2.5 fallbacks) are left unpriced
# on purpose rather than guessed at.
GEMINI_TEXT_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
}
GEMINI_TEXT_LONG_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.1-pro-preview": (4.00, 18.00),
}
GEMINI_LONG_CONTEXT_THRESHOLD = 200_000

# Image models bill input and image-output tokens separately. The per-image
# token counts are Google's published constants; we prefer the real
# usage_metadata when the SDK reports it and fall back to these.
GEMINI_IMAGE_RATES: dict[str, dict[str, float]] = {
    # Nano Banana Pro
    "gemini-3-pro-image-preview": {"in_per_mtok": 2.00, "img_out_per_mtok": 120.00},
    "gemini-3-pro-image": {"in_per_mtok": 2.00, "img_out_per_mtok": 120.00},
    # Nano Banana 2
    "gemini-3.1-flash-image-preview": {"in_per_mtok": 0.50, "img_out_per_mtok": 60.00},
    "gemini-3.1-flash-image": {"in_per_mtok": 0.50, "img_out_per_mtok": 60.00},
    "gemini-3.1-flash-lite-image": {"in_per_mtok": 0.50, "img_out_per_mtok": 60.00},
}
# Output image tokens by rendered size. NB Pro publishes one figure for the
# whole 1K-2K band; NB2 publishes a per-step ladder.
_PRO_IMAGE_TOKENS: dict[str, int] = {"1K": 1120, "2K": 1120, "4K": 2000}
_FLASH_IMAGE_TOKENS: dict[str, int] = {
    "512": 747, "0.5K": 747, "1K": 1120, "2K": 1680, "4K": 2520,
}

# gemini-omni-flash-preview: output video bills at $17.50/1M tokens and 720p
# video is 5792 tokens per second — Google's own "approximately $0.10/second".
OMNI_VIDEO_PER_MTOK = 17.50
OMNI_TOKENS_PER_SECOND = 5792

# EvoLink. Only used when a task response somehow lacks usage.cost.usd; the
# per-credit rate is confirmed by division against live task data
# (54 credits -> $0.795).
EVOLINK_USD_PER_CREDIT = 0.014722
EVOLINK_CREDITS_PER_SECOND: dict[str, float] = {"seedance": 13.5, "kling": 5.4}
# Omni resold on EvoLink is quoted in DOLLARS per output second, not credits
# (token-metered upstream: $0.015/1K video output tokens x 5792 tok/s), so it
# cannot ride the credits table without inventing a credit figure that would
# then get printed as if EvoLink had charged it. Estimate-only, same as above.
# $0.015/1K video output tokens x 5792 tokens/s = $0.0869/s (EvoLink, 2026-08-05).
EVOLINK_USD_PER_SECOND: dict[str, float] = {"omni_evolink": 0.087}

# elevenlabs.io/pricing/api — USD per 1000 characters.
ELEVENLABS_PER_KCHAR: dict[str, float] = {
    "eleven_v3": 0.10,
    "eleven_multilingual_v2": 0.10,
    "eleven_flash_v2_5": 0.05,
    "eleven_turbo_v2_5": 0.05,
}
ELEVENLABS_DEFAULT_PER_KCHAR = 0.10
# Scribe speech-to-text, used for forced alignment. USD per hour of audio.
ELEVENLABS_SCRIBE_PER_HOUR = 0.22

# sync.so/pricing — base tier, USD per second of output video.
SYNCSO_PER_SECOND = 0.05
# replicate.com/heygen/lipsync-precision — USD per second of output video.
REPLICATE_HEYGEN_PER_SECOND = 0.0667

# Providers we deliberately record at zero rather than leave unpriced.
FREE_MODELS: frozenset[str] = frozenset({"s2.1-pro-free"})


# ── Ledger ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CostEntry:
    """One priced provider call."""
    component: str          # video_clips | lipsync | images | voice | llm
    provider: str
    model: str
    unit: str               # s | char | image | token | credit
    qty: float
    usd: float
    authoritative: bool = False
    wasted_usd: float = 0.0
    ref: str | None = None  # provider-side id, when there is one
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "component": self.component, "provider": self.provider,
            "model": self.model, "unit": self.unit,
            "qty": round(self.qty, 4), "usd": round(self.usd, 6),
            "authoritative": self.authoritative,
        }
        if self.wasted_usd:
            d["wasted_usd"] = round(self.wasted_usd, 6)
        if self.ref:
            d["ref"] = self.ref
        if self.detail:
            d["detail"] = self.detail
        return d


class CostLedger:
    """Append-only accumulator for one run's spend.

    Lock-guarded because the voice/image/lipsync clients run some work through
    ``asyncio.to_thread``; the lock costs nothing and removes the question.
    ``dedupe_key`` exists for the resume path: polling an already-completed
    EvoLink task returns its cost again, and re-recording it would invent spend
    that never happened.
    """

    def __init__(self) -> None:
        self._entries: list[CostEntry] = []
        self._seen: set[str] = set()
        self._unpriced: list[str] = []
        self._lock = threading.Lock()

    def add(self, entry: CostEntry, *, dedupe_key: str | None = None) -> None:
        with self._lock:
            if dedupe_key is not None:
                if dedupe_key in self._seen:
                    return
                self._seen.add(dedupe_key)
            self._entries.append(entry)

    def note_unpriced(self, what: str) -> None:
        with self._lock:
            if what not in self._unpriced:
                self._unpriced.append(what)

    @property
    def entries(self) -> list[CostEntry]:
        with self._lock:
            return list(self._entries)

    @property
    def total_usd(self) -> float:
        return sum(e.usd for e in self.entries)

    @property
    def wasted_usd(self) -> float:
        return sum(e.wasted_usd for e in self.entries)

    def by_component(self) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for e in self.entries:
            a = agg.setdefault(e.component, {
                "usd": 0.0, "wasted_usd": 0.0, "qty": 0.0,
                "unit": e.unit, "calls": 0, "providers": [],
            })
            a["usd"] += e.usd
            a["wasted_usd"] += e.wasted_usd
            a["calls"] += 1
            # Mixed units inside one component (omni seconds vs seedance
            # seconds is fine; tokens vs images is not) — blank the unit rather
            # than sum incomparable quantities into a misleading number.
            if a["unit"] == e.unit:
                a["qty"] += e.qty
            else:
                a["unit"] = ""
                a["qty"] = 0.0
            if e.provider not in a["providers"]:
                a["providers"].append(e.provider)
        for a in agg.values():
            a["usd"] = round(a["usd"], 6)
            a["wasted_usd"] = round(a["wasted_usd"], 6)
            a["qty"] = round(a["qty"], 4)
        return agg

    def snapshot(self) -> dict:
        """The JSON blob written to the run manifest."""
        return {
            "total_usd": round(self.total_usd, 4),
            "wasted_usd": round(self.wasted_usd, 4),
            "currency": "USD",
            "priced_at": datetime.now(timezone.utc).isoformat(),
            "rate_table_version": RATE_TABLE_VERSION,
            "by_component": self.by_component(),
            "entries": [e.to_dict() for e in self.entries],
            "unpriced": list(self._unpriced),
        }


# ── Active-ledger plumbing ───────────────────────────────────────────────

_ACTIVE: ContextVar["CostLedger | None"] = ContextVar("cost_ledger", default=None)


def active() -> "CostLedger | None":
    return _ACTIVE.get()


def start_run() -> CostLedger:
    """Install a fresh ledger for the current async context and return it.

    Called at the top of a pipeline run rather than wrapped around it: a
    coroutine's ContextVar writes are scoped to the Task it runs in, and every
    caller (Gradio's per-request task, ``asyncio.run`` in scripts) gives the
    run its own Task. Installing unconditionally also means a second run can
    never inherit the first one's ledger.
    """
    ledger = CostLedger()
    _ACTIVE.set(ledger)
    return ledger


@contextlib.contextmanager
def activate(ledger: CostLedger) -> Iterator[CostLedger]:
    token = _ACTIVE.set(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE.reset(token)


def _emit(entry: CostEntry, *, dedupe_key: str | None = None) -> None:
    led = _ACTIVE.get()
    if led is not None:
        led.add(entry, dedupe_key=dedupe_key)


def note_unpriced(what: str) -> None:
    led = _ACTIVE.get()
    if led is not None:
        led.note_unpriced(what)


# ── Pricing + recording, one pair per provider ───────────────────────────

def _num(obj: Any, key: str) -> float:
    """Read a usage counter off an SDK object or dict, defaulting to 0."""
    v = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def price_claude(model: str, usage: Any) -> "tuple[float, dict] | None":
    """USD for one Claude call from its usage object. None if unrecognised.

    ``input_tokens`` is the UNCACHED remainder only — cached tokens are
    reported separately and billed at their own multipliers, so all three are
    summed rather than treating input_tokens as the whole prompt.
    """
    rates = CLAUDE_PER_MTOK.get(model)
    if rates is None:
        return None
    in_rate, out_rate = rates
    fresh = _num(usage, "input_tokens")
    cache_read = _num(usage, "cache_read_input_tokens")
    cache_write = _num(usage, "cache_creation_input_tokens")
    out = _num(usage, "output_tokens")
    usd = (
        fresh * in_rate
        + cache_write * in_rate * CACHE_WRITE_MULT
        + cache_read * in_rate * CACHE_READ_MULT
        + out * out_rate
    ) / 1_000_000
    detail = {
        "input_tokens": int(fresh), "cache_read_tokens": int(cache_read),
        "cache_write_tokens": int(cache_write), "output_tokens": int(out),
    }
    return usd, detail


def record_claude(model: str, usage: Any) -> None:
    priced = price_claude(model, usage)
    if priced is None:
        note_unpriced(f"claude:{model}")
        return
    usd, detail = priced
    total_tok = sum(detail.values())
    _emit(CostEntry(
        component="llm", provider="anthropic", model=model,
        unit="token", qty=total_tok, usd=usd, detail=detail,
    ))


def price_gemini_text(model: str, usage: Any) -> "tuple[float, dict] | None":
    """USD for one Gemini text call from its usage_metadata."""
    prompt = _num(usage, "prompt_token_count")
    cached = _num(usage, "cached_content_token_count")
    # Gemini folds thinking tokens into the billed output count.
    out = (_num(usage, "candidates_token_count")
           + _num(usage, "thoughts_token_count"))
    table = (GEMINI_TEXT_LONG_PER_MTOK
             if prompt > GEMINI_LONG_CONTEXT_THRESHOLD
             and model in GEMINI_TEXT_LONG_PER_MTOK
             else GEMINI_TEXT_PER_MTOK)
    rates = table.get(model)
    if rates is None:
        return None
    in_rate, out_rate = rates
    usd = (prompt * in_rate + out * out_rate) / 1_000_000
    return usd, {
        "prompt_tokens": int(prompt), "cached_tokens": int(cached),
        "output_tokens": int(out),
    }


def record_gemini_text(model: str, usage: Any) -> None:
    priced = price_gemini_text(model, usage)
    if priced is None:
        note_unpriced(f"gemini-text:{model}")
        return
    usd, detail = priced
    _emit(CostEntry(
        component="llm", provider="google", model=model, unit="token",
        qty=detail["prompt_tokens"] + detail["output_tokens"],
        usd=usd, detail=detail,
    ))


def image_output_tokens(model: str, size: str) -> int:
    """Published output-image token count for a model at a rendered size."""
    table = _PRO_IMAGE_TOKENS if "pro-image" in model else _FLASH_IMAGE_TOKENS
    key = (size or "").upper().replace("PX", "")
    return table.get(key, table.get("2K", 1680))


def price_gemini_image(model: str, size: str,
                       usage: Any = None) -> "tuple[float, dict] | None":
    """USD for one generated image.

    Output-image tokens come from the published per-size constants: the SDK's
    usage_metadata reports a single candidates_token_count that mixes image and
    any text/thinking tokens, so it cannot be split reliably. Input tokens
    (prompt + reference images) DO come from usage_metadata when present, which
    matters here — the anchor path sends up to two reference images per call.
    """
    rates = GEMINI_IMAGE_RATES.get(model)
    if rates is None:
        return None
    img_tokens = image_output_tokens(model, size)
    in_tokens = _num(usage, "prompt_token_count") if usage is not None else 0.0
    usd = (in_tokens * rates["in_per_mtok"]
           + img_tokens * rates["img_out_per_mtok"]) / 1_000_000
    return usd, {
        "size": size, "image_output_tokens": img_tokens,
        "prompt_tokens": int(in_tokens),
    }


def record_gemini_image(model: str, size: str, usage: Any = None) -> None:
    priced = price_gemini_image(model, size, usage)
    if priced is None:
        note_unpriced(f"gemini-image:{model}")
        return
    usd, detail = priced
    _emit(CostEntry(
        component="images", provider="google", model=model,
        unit="image", qty=1, usd=usd, detail=detail,
    ))


def price_omni_video(seconds: float) -> float:
    return seconds * OMNI_TOKENS_PER_SECOND * OMNI_VIDEO_PER_MTOK / 1_000_000


def price_evolink(raw: Any, *, provider: str = "seedance",
                  seconds: float = 0.0) -> "tuple[float, bool]":
    """(usd, authoritative) for one EvoLink task.

    A completed task carries ``usage.cost.usd`` — the amount EvoLink billed,
    which is the ground truth and needs no rate of ours. The credit and
    per-second fallbacks exist only for a response shape that omits it.
    """
    usage = (raw or {}).get("usage") if isinstance(raw, dict) else None
    if isinstance(usage, dict):
        cost = usage.get("cost")
        if isinstance(cost, dict) and cost.get("usd") is not None:
            return float(cost["usd"]), True
        if usage.get("credits_used") is not None:
            return float(usage["credits_used"]) * EVOLINK_USD_PER_CREDIT, False
    if provider in EVOLINK_USD_PER_SECOND:
        return seconds * EVOLINK_USD_PER_SECOND[provider], False
    cps = EVOLINK_CREDITS_PER_SECOND.get(provider, 0.0)
    return seconds * cps * EVOLINK_USD_PER_CREDIT, False


def record_clip(*, task_id: str, provider: str, model: str,
                ordered_seconds: float, used_seconds: float,
                raw: Any = None) -> None:
    """Record one rendered clip, including the seconds paid for but trimmed.

    ``used_seconds`` is the beat window; anything ordered beyond it is real
    money spent on frames that never reach the cut (the 4s-floor tax). Omni
    bills in Google dollars rather than EvoLink credits, so it prices off its
    own per-second rate.
    """
    if provider == "omni":
        usd, authoritative = price_omni_video(ordered_seconds), False
    else:
        usd, authoritative = price_evolink(
            raw, provider=provider, seconds=ordered_seconds)
    used = max(0.0, min(used_seconds, ordered_seconds))
    wasted = usd * (1 - used / ordered_seconds) if ordered_seconds > 0 else 0.0
    _emit(
        CostEntry(
            component="video_clips", provider=provider, model=model,
            unit="s", qty=ordered_seconds, usd=usd,
            authoritative=authoritative, wasted_usd=wasted, ref=task_id,
            detail={"used_s": round(used, 2)},
        ),
        # Resume re-polls a completed task; its cost was already paid once.
        dedupe_key=f"clip:{task_id}",
    )


def record_tts(*, provider: str, model: str, characters: int) -> None:
    if model in FREE_MODELS:
        _emit(CostEntry(component="voice", provider=provider, model=model,
                        unit="char", qty=characters, usd=0.0))
        return
    if provider != "elevenlabs":
        # Cartesia and Fish paid tiers have no rate we have verified.
        note_unpriced(f"tts:{provider}:{model}")
        return
    rate = ELEVENLABS_PER_KCHAR.get(model, ELEVENLABS_DEFAULT_PER_KCHAR)
    _emit(CostEntry(
        component="voice", provider=provider, model=model,
        unit="char", qty=characters, usd=characters * rate / 1000,
    ))


def record_stt(*, model: str, seconds: float) -> None:
    """Scribe transcription / forced alignment, billed by audio duration."""
    _emit(CostEntry(
        component="voice", provider="elevenlabs", model=model,
        unit="s", qty=seconds, usd=seconds / 3600 * ELEVENLABS_SCRIBE_PER_HOUR,
        detail={"billed_as": "scribe_audio_hours"},
    ))


def record_lipsync(*, provider: str, model: str, seconds: float,
                   ref: str | None = None) -> None:
    if provider == "replicate":
        rate = REPLICATE_HEYGEN_PER_SECOND
    elif provider == "syncso":
        rate = SYNCSO_PER_SECOND
    else:
        note_unpriced(f"lipsync:{provider}:{model}")
        return
    _emit(CostEntry(
        component="lipsync", provider=provider, model=model,
        unit="s", qty=seconds, usd=seconds * rate, ref=ref,
    ))


# ── Rendering ────────────────────────────────────────────────────────────

_COMPONENT_LABELS: dict[str, str] = {
    "video_clips": "Video clips",
    "lipsync": "Lipsync",
    "images": "Images",
    "voice": "Voice",
    "llm": "LLM",
}
_COMPONENT_ORDER = ("video_clips", "lipsync", "images", "voice", "llm")


def _fmt_qty(qty: float, unit: str) -> str:
    if not unit:
        return "—"
    if unit == "s":
        return f"{qty:.1f}s"
    if unit == "token":
        return f"{qty / 1000:.0f}k tok"
    if unit == "char":
        return f"{int(qty)} ch"
    if unit == "image":
        return f"{int(qty)} img"
    return f"{qty:g} {unit}"


def _ordered(by_component: dict) -> list[tuple[str, dict]]:
    known = [(k, by_component[k]) for k in _COMPONENT_ORDER if k in by_component]
    extra = sorted((k, v) for k, v in by_component.items()
                   if k not in _COMPONENT_ORDER)
    return known + extra


def format_line(snapshot: dict) -> str:
    """One-line total for the progress log."""
    waste = snapshot.get("wasted_usd") or 0.0
    line = f"total cost ${snapshot['total_usd']:.2f}"
    if waste >= 0.005:
        line += f" (${waste:.2f} paid for output that was trimmed or retried)"
    return line


def format_markdown(snapshot: dict) -> str:
    """The per-component table shown in the UI's Cost accordion."""
    if not snapshot or not snapshot.get("entries"):
        return "*No priced calls recorded.*"
    rows = [
        "| Component | Provider | Usage | Cost |",
        "|---|---|---:|---:|",
    ]
    for key, agg in _ordered(snapshot["by_component"]):
        rows.append(
            f"| {_COMPONENT_LABELS.get(key, key)} "
            f"| {', '.join(agg['providers'])} "
            f"| {_fmt_qty(agg['qty'], agg['unit'])} "
            f"| ${agg['usd']:.2f} |"
        )
    rows.append(f"| **Total** | | | **${snapshot['total_usd']:.2f}** |")
    out = "\n".join(rows)

    waste = snapshot.get("wasted_usd") or 0.0
    if waste >= 0.005:
        pct = waste / snapshot["total_usd"] * 100 if snapshot["total_usd"] else 0
        out += (
            f"\n\n**Waste ${waste:.2f}** ({pct:.0f}%) — seconds ordered from the "
            "provider that the final cut trimmed away, plus any clip that was "
            "paid for and then re-ordered."
        )
    clips = snapshot["by_component"].get("video_clips")
    if clips and clips.get("qty"):
        used = sum(e.get("detail", {}).get("used_s", 0)
                   for e in snapshot["entries"]
                   if e["component"] == "video_clips")
        out += f"\n\nVideo: {clips['qty']:.1f}s ordered → {used:.1f}s used."
    if snapshot.get("unpriced"):
        out += ("\n\n*Not priced (no verified rate): "
                + ", ".join(snapshot["unpriced"]) + "*")
    out += (f"\n\n<sub>Rate table {snapshot['rate_table_version']}. EvoLink "
            "clips are billed at the exact figure EvoLink reports; everything "
            "else is metered usage at published rates.</sub>")
    return out
