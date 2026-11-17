"""Google Gemini Nano Banana image generation client.

Uses the google-genai SDK directly (no REST wrapping). Both sync and
async paths are supported so callers can pick:

  - ``generate_sync`` — used by the Gradio gallery tab (sync handler).
  - ``generate_async`` — used by the workflow tab (async handler) so
    multiple anchor images can be generated in parallel via
    ``asyncio.gather``.

Both paths share the same config builder and the same S3-upload step.

Tier-based model selection:
  - "hero"   → Nano Banana Pro (gemini-3-pro-image-preview). Highest
               pure photorealism. The LIVE headless pipeline
               (app/pipeline.py) renders the character ref AND every per-beat
               anchor on this tier — Pro's identity/product preservation
               dominates the small cost delta vs Seedance.
  - "anchor" → Nano Banana 2 (gemini-3.1-flash-image-preview) with
               thinking_level=high. Cheaper/faster; creator-content aesthetic.
               Used only by the Gradio gallery / FLF2V UI path — NOT by the
               live pipeline. (Pro-vs-NB2 for anchors is an open A/B.)

S3 uploads reuse ``app.uploader.S3Uploader`` so all generated images land
in the same content-addressable prefix as user-uploaded assets.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app import costs
from app.config import IMAGE_FALLBACK_MODELS, NB2_MODEL, NB_PRO_MODEL, Settings
from app.uploader import S3Uploader

ImageTier = Literal["hero", "anchor"]

# Default Nano Banana imageSize (gemini-3 image preview, "2K" = 1536x2752 @ 9:16).
# NOTE: the "2K"/"4K" upscale path had an INTERMITTENT server-side regression
# around 2026-06 that returned hazy/fogged/vignetted/double-exposed images; it
# recovered by 2026-06-20. If fogged renders reappear, drop this to "1K" (native
# ~768x1376, always crisp) and re-test with scripts/charref_eval.py.
DEFAULT_IMAGE_SIZE = "2K"

# ─── Reference image cache (per-process) ──────────────────────────────
# Anchor renders fan out 10-wide; each one used to refetch the same
# char_ref + product_ref from S3. We cache the bytes after the first
# successful fetch so the other 9 calls reuse them. Locks per URL prevent
# a thundering-herd of parallel downloads on cold cache.
_REF_BYTES_CACHE: dict[str, tuple[bytes, str]] = {}   # url -> (bytes, mime)
_REF_LOCKS: dict[str, asyncio.Lock] = {}


async def _fetch_reference_with_retry(url: str) -> tuple[bytes, str]:
    """Download a reference image with retry, caching on success.

    Called by ``_build_contents``. Returns ``(bytes, mime)``.

    DECISION POINT — retry policy lives here. See the TODO inside.
    """
    if url in _REF_BYTES_CACHE:
        return _REF_BYTES_CACHE[url]
    lock = _REF_LOCKS.setdefault(url, asyncio.Lock())
    async with lock:
        if url in _REF_BYTES_CACHE:   # another task filled it while we waited
            return _REF_BYTES_CACHE[url]

        import httpx

        # Retry policy: 4 attempts, exponential backoff (0.5s, 1s, 2s, 4s),
        # ~7.5s total worst-case. Retries on connect / read / TLS errors and
        # 5xx; gives up on 4xx (those are permanent — bad URL, dead object).
        # On permanent failure, RAISE — silently rendering an anchor without
        # the identity reference would produce drift that's worse than a
        # clear error the manifest can capture.
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            for attempt in range(4):
                try:
                    resp = await http.get(url)
                    if 500 <= resp.status_code < 600:
                        raise httpx.HTTPStatusError(
                            f"server {resp.status_code}", request=resp.request, response=resp,
                        )
                    resp.raise_for_status()
                    raw_mime = resp.headers.get("content-type") or "image/jpeg"
                    if "png" in raw_mime:
                        mime = "image/png"
                    elif "webp" in raw_mime:
                        mime = "image/webp"
                    else:
                        mime = "image/jpeg"
                    _REF_BYTES_CACHE[url] = (resp.content, mime)
                    return _REF_BYTES_CACHE[url]
                except httpx.HTTPStatusError as e:
                    # 4xx is permanent — bad URL, expired signed URL, etc.
                    if 400 <= e.response.status_code < 500:
                        raise
                    last_exc = e
                except (httpx.ConnectError, httpx.ReadError,
                        httpx.ReadTimeout, httpx.ConnectTimeout,
                        httpx.RemoteProtocolError) as e:
                    last_exc = e
                if attempt < 3:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(
            f"failed to fetch reference image after 4 attempts: {url}"
        ) from last_exc


@dataclass(frozen=True)
class GeneratedImage:
    id: str
    url: str
    prompt: str
    size: str            # Nano Banana imageSize enum, not WxH (e.g. "2K")
    source: str          # "tab3" | "workflow"
    created_at: str      # ISO8601

    @classmethod
    def from_url(cls, *, url: str, prompt: str, size: str, source: str) -> "GeneratedImage":
        img_id = hashlib.sha256(url.encode()).hexdigest()[:16]
        return cls(
            id=img_id, url=url, prompt=prompt,
            size=size, source=source,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GeneratedImage":
        return cls(**d)


# ─── Reference-image citations ────────────────────────────────────────
# Prefixed onto the prompt so the model knows what each attached image is.
# The citation MUST be chosen by what the references ARE (role), not by how
# many there are: a product-only B-roll beat passes a single image that is the
# PRODUCT, and must NOT be told "image 1 is the character" (that instructed the
# model to render a person matching a product pouch — a real defect).

_CITATION_CHARACTER = (
    "CRITICAL — IMAGE 1 IS THE CHARACTER. The person rendered in "
    "this frame MUST be the SAME person as in image 1 — match "
    "the face shape, eyes, eyebrows, nose, lips, jawline, skin "
    "tone, hair (colour/texture/length), body shape, wardrobe, "
    "and jewellery EXACTLY. Do NOT reinterpret features. Do NOT "
    "change age, ethnicity, or build. If unsure, copy image 1's "
    "face pixel-for-pixel. "
)

# Shared product-fidelity clauses.
#
# LEAK WARNING: never put slogan-shaped ALL-CAPS tokens (e.g. the old
# "PIXEL-ACCURATE") in the same breath as label/text instructions — a live
# anchor rendered a garment tag that literally read "PIXEL-ACCURATE / M /
# MADE IN C2OLAN". Plain words ("exact", "faithful") carry the same weight
# without getting transcribed onto the product.
_NO_INSTRUCTION_TEXT = (
    "Never print any words from these instructions inside the image — "
    "rendered text may come ONLY from what is visible in the reference "
    "images. "
)
# The canonical apparel reference is a folded/laid-flat studio shot. Without
# this clause, "same exact form" makes the model reproduce the flat-lay
# silhouette ON the body (a stitched-cushion effect, fold crease included)
# instead of re-draping the fabric.
_WORN_APPAREL_DRAPE = (
    "If the product is a garment and this scene shows it being worn, it must "
    "hang and drape naturally on the body — real gravity, soft folds, fabric "
    "tension at contact points. Match the reference's fabric, colour, cut, "
    "and branding, never its folded or laid-flat silhouette. "
)

_CITATION_PRODUCT_ONLY = (
    "CRITICAL — IMAGE 1 IS THE PRODUCT. This is a B-roll / product shot — "
    "NO person's face is the subject (hands may appear if the scene calls "
    "for them). Reproduce the product in image 1 exactly: same product type "
    "and form, shape, colour, "
    "material/fabric, and any label/logo artwork. Do NOT invent a different "
    "variant, colour, or form, and do NOT add, move, restyle, or letter any "
    "logo, label, or text — reproduce ONLY the branding visibly present in "
    "image 1 (if the reference shows NO visible logo or text, add none). "
    "Render the product item itself, NOT any retail box or outer packaging. "
    + _WORN_APPAREL_DRAPE + _NO_INSTRUCTION_TEXT
)

# IMAGE 1 is the SOURCE PRODUCT PHOTO (the user's own asset). This is an EDIT
# instruction, NOT a pixel-lock — the model should clean up the shot (drop the
# carton/box) while keeping the item's branding pixel-accurate.
_CITATION_PRODUCT_SOURCE = (
    "IMAGE 1 IS THE SOURCE PRODUCT PHOTO. Output a clean studio product shot "
    "of ONLY the product item itself — REMOVE any carton, box, outer "
    "packaging, or duplicate. Plain light seamless background, the product "
    "displayed naturally for its type (folded or laid flat for apparel, "
    "upright for a container), label and logo (if any) facing the "
    "camera, soft even lighting, no glare or reflections, not mirrored. "
    "PRESERVE the item's exact shape, colour, material/fabric, label artwork, "
    "logo, and ALL text unchanged — do not redesign, re-letter, "
    "recolour, or invent branding (if the reference shows no logo or text, "
    "add none). " + _NO_INSTRUCTION_TEXT
)

# Assumes the caller passes references in [character, product] order (the live
# pipeline always appends the character ref first, then the product).
_CITATION_CHARACTER_AND_PRODUCT = (
    "CRITICAL — TWO REFERENCE IMAGES.\n"
    "IMAGE 1 IS THE CHARACTER. Render the SAME person as image 1 "
    "— match face shape, eyes, eyebrows, nose, lips, jawline, "
    "skin tone, hair (colour/texture/length), body shape, "
    "wardrobe, jewellery EXACTLY. Do NOT reinterpret features. "
    "Do NOT change age, ethnicity, or build.\n"
    "IMAGE 2 IS THE PRODUCT. When the product appears in this "
    "scene, render it as an exact reproduction "
    "of image 2 — same product type and form, shape, colour, "
    "material/fabric, and any label/logo artwork. Do NOT invent a "
    "different variant, and do NOT change the product's form "
    "(apparel stays apparel, a pouch stays a pouch, a container "
    "stays a container). Do NOT change its colour, or add/move/letter any "
    "logo, label, or text not visibly present in image 2 (if it "
    "shows no branding, add none). Render the product item itself, "
    "NOT any retail box or outer packaging. "
    + _WORN_APPAREL_DRAPE + _NO_INSTRUCTION_TEXT + "\n"
)


# Hands-only reference: a B-roll cutaway that shows the CREATOR'S hands (using
# the product, pouring, unboxing) but NOT her face. Image 1 is the character
# ref, used purely to match hands/skin tone — never to render the face.
_CITATION_HANDS = (
    "CRITICAL — IMAGE 1 SHOWS THE CREATOR (hands & skin-tone reference ONLY). "
    "This is a B-roll cutaway with NO face in frame. Any hands shown MUST match "
    "the person in image 1 — same skin tone, hand shape, nails, and any wrist "
    "jewellery or sleeves. Do NOT render her face. Do NOT show a different "
    "person's hands. "
)
_CITATION_HANDS_AND_PRODUCT = (
    _CITATION_HANDS +
    "IMAGE 2 IS THE PRODUCT — reproduce it exactly "
    "(same product type and form, shape, colour, material/fabric, and any "
    "label/logo artwork); do NOT invent a different variant or form, and do "
    "NOT add any logo or text not visibly present in image 2. Render the "
    "product item itself, NOT any retail box or outer packaging. "
    + _WORN_APPAREL_DRAPE + _NO_INSTRUCTION_TEXT
)


def _citation_for(reference_roles: list[str] | None, n_refs: int) -> str:
    """Pick the citation by what the refs ARE, not how many there are.

    ``reference_roles`` is a list like ``["character"]`` / ``["product"]`` /
    ``["product_source"]`` / ``["character", "product"]`` /
    ``["character_hands"]`` / ``["character_hands", "product"]`` aligned with
    the reference image order.
    When None (e.g. the gallery tab passes raw URLs), fall back to the legacy
    index convention: 1 ref = character, 2+ = character + product.
    """
    if reference_roles:
        # product_source is an EDIT job (clean up the user's own photo), not a
        # render — recognize it BEFORE the 1-ref=character fallback.
        if "product_source" in reference_roles:
            return _CITATION_PRODUCT_SOURCE
        has_hands = "character_hands" in reference_roles
        has_char = "character" in reference_roles
        has_prod = "product" in reference_roles
        if has_hands:
            return _CITATION_HANDS_AND_PRODUCT if has_prod else _CITATION_HANDS
        if has_char and has_prod:
            return _CITATION_CHARACTER_AND_PRODUCT
        if has_prod and not has_char:
            return _CITATION_PRODUCT_ONLY
        return _CITATION_CHARACTER
    return _CITATION_CHARACTER if n_refs == 1 else _CITATION_CHARACTER_AND_PRODUCT


class NanoBananaImageGenerator:
    """Tier-aware Gemini Nano Banana client. All outputs resolved to S3 URLs."""

    def __init__(self, settings: Settings, uploader: S3Uploader,
                 *, _client: Any = None):
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if _client is not None:
            self._client = _client
        else:
            from google import genai
            self._client = genai.Client(api_key=settings.gemini_api_key)
        self._uploader = uploader

    # ─── public API ────────────────────────────────────────────────

    def generate_sync(
        self, *, prompt: str,
        tier: ImageTier = "anchor",
        size: str = DEFAULT_IMAGE_SIZE,
        aspect_ratio: str = "9:16",
        n: int = 1,
        source: str = "workflow",
    ) -> list[GeneratedImage]:
        """Sync image generation via the google-genai SDK.

        Used by the Gradio gallery tab and the standalone CLI script.
        For workflow paths, prefer ``generate_async``.
        """
        model = self._model_for_tier(tier)
        config = self._config_for_tier(tier, size=size, aspect_ratio=aspect_ratio)
        results: list[GeneratedImage] = []
        for _ in range(n):
            resp, used_model, used_size = self._generate_sync_with_fallback(
                model=model, contents=prompt, config=config,
            )
            results.append(self._extract_image(
                resp, prompt=prompt, size=used_size, source=source,
                model=used_model,
            ))
        return results

    async def generate_async(
        self, *, prompt: str,
        tier: ImageTier = "anchor",
        size: str = DEFAULT_IMAGE_SIZE,
        aspect_ratio: str = "9:16",
        n: int = 1,
        source: str = "workflow",
        reference_image_urls: list[str] | None = None,
        reference_roles: list[str] | None = None,
    ) -> list[GeneratedImage]:
        """Async image generation via the google-genai SDK.

        Used by the workflow tab so multiple anchor images can fire in
        parallel via ``asyncio.gather``. S3 upload is synchronous (boto3
        is not natively async), but each upload is ~200ms compared to
        ~15s per Nano Banana call, so the bottleneck stays the API call.

        ``reference_image_urls``: optional list of public image URLs to
        pass as multi-image input. When provided, each URL is downloaded
        and added to the SDK's ``contents`` list as a Part. The prompt
        is automatically prefixed with a citation instructing the model
        to use ``image 1`` (etc) as the character identity. This is the
        identity-locking mechanism — NB2 sees the character ref image
        and renders chunk anchors that match its face/hair/wardrobe.
        """
        model = self._model_for_tier(tier)
        config = self._config_for_tier(tier, size=size, aspect_ratio=aspect_ratio)
        contents = await self._build_contents(
            prompt, reference_image_urls, reference_roles,
        )
        results: list[GeneratedImage] = []
        for _ in range(n):
            resp, used_model, used_size = await self._generate_async_with_fallback(
                model=model, contents=contents, config=config,
            )
            results.append(self._extract_image(
                resp, prompt=prompt, size=used_size, source=source,
                model=used_model,
            ))
        return results

    # ─── model fallback ─────────────────────────────────────────────
    #
    # The SDK retries transient 5xx on the SAME model (tenacity, built in).
    # When those retries exhaust — the model itself is overloaded (503
    # "high demand") — retrying it further just burns wall-clock. Instead
    # we walk IMAGE_FALLBACK_MODELS. Only ServerError (5xx) triggers a
    # fallback; ClientError (bad request, auth, quota) fails fast because
    # a different model won't fix those — with ONE exception: the primary's
    # config may be invalid on a fallback model (flash-lite 400s on
    # image_size="2K"), which is a computable config mismatch, not a bad
    # request. That case downgrades to native "1K" and retries the SAME
    # model. Returns (resp, model_used, size_used) so metadata records what
    # actually rendered.

    def _generate_sync_with_fallback(self, *, model: str, contents, config):
        from google.genai import errors
        chain = self._model_chain(model)
        last_exc: Exception | None = None
        for i, candidate in enumerate(chain):
            cand_config = config
            while True:
                try:
                    resp = self._client.models.generate_content(
                        model=candidate, contents=contents, config=cand_config,
                    )
                    return resp, candidate, cand_config.image_config.image_size
                except errors.ServerError as exc:
                    last_exc = exc
                    self._warn_fallback(candidate, chain, i, exc)
                    break
                except errors.ClientError as exc:
                    cand_config = self._downgrade_size(cand_config, exc, candidate)
                    if cand_config is None:
                        raise
        raise last_exc  # type: ignore[misc]

    async def _generate_async_with_fallback(self, *, model: str, contents, config):
        from google.genai import errors
        chain = self._model_chain(model)
        last_exc: Exception | None = None
        for i, candidate in enumerate(chain):
            cand_config = config
            while True:
                try:
                    resp = await self._client.aio.models.generate_content(
                        model=candidate, contents=contents, config=cand_config,
                    )
                    return resp, candidate, cand_config.image_config.image_size
                except errors.ServerError as exc:
                    last_exc = exc
                    self._warn_fallback(candidate, chain, i, exc)
                    break
                except errors.ClientError as exc:
                    cand_config = self._downgrade_size(cand_config, exc, candidate)
                    if cand_config is None:
                        raise
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _downgrade_size(config, exc: Exception, candidate: str):
        """Repair an 'Image size X is not supported for this model' 400 by
        rebuilding the config at native "1K" (supported everywhere, always
        crisp). Returns the downgraded config, or None when the error is a
        different ClientError / the config is already at 1K — the caller
        re-raises (fail fast) in that case."""
        msg = str(getattr(exc, "message", "") or exc).lower()
        if not ("image size" in msg and "not supported" in msg):
            return None
        img = getattr(config, "image_config", None)
        if img is None or img.image_size in (None, "1K"):
            return None
        from google.genai import types
        print(f"[image_gen] {candidate} rejects image_size={img.image_size} "
              f"— retrying at 1K")
        return config.model_copy(update={
            "image_config": types.ImageConfig(
                aspect_ratio=img.aspect_ratio, image_size="1K",
            ),
        })

    @staticmethod
    def _model_chain(model: str) -> list[str]:
        return [model] + [m for m in IMAGE_FALLBACK_MODELS if m != model]

    @staticmethod
    def _warn_fallback(candidate: str, chain: list[str], i: int,
                       exc: Exception) -> None:
        nxt = chain[i + 1] if i + 1 < len(chain) else None
        if nxt:
            print(f"[image_gen] {candidate} unavailable "
                  f"({getattr(exc, 'code', '?')}) — falling back to {nxt}")

    @staticmethod
    async def _build_contents(prompt: str,
                              reference_image_urls: list[str] | None,
                              reference_roles: list[str] | None = None):
        """Build the SDK ``contents`` list, downloading reference images.

        Returns either a plain string (no references) or a list of
        [prefixed_prompt, Part, Part, ...] when references are present.

        The citation is chosen by what the references ARE (``reference_roles``),
        NOT by how many there are — a product-only B-roll beat must be told
        "image 1 is the PRODUCT", never "image 1 is the character".
        """
        if not reference_image_urls:
            return prompt

        from google.genai import types

        citation = _citation_for(reference_roles, len(reference_image_urls))
        parts: list = [citation + prompt]

        for url in reference_image_urls:
            data, mime = await _fetch_reference_with_retry(url)
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        return parts

    # ─── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _model_for_tier(tier: ImageTier) -> str:
        return NB_PRO_MODEL if tier == "hero" else NB2_MODEL

    @staticmethod
    def _config_for_tier(tier: ImageTier, *, size: str, aspect_ratio: str):
        """Build GenerateContentConfig for the given tier.

        NB Pro does not support ``thinking_config``; NB2 does.
        """
        from google.genai import types
        kwargs: dict = dict(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=size,
            ),
        )
        if tier == "anchor":
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="high")
        return types.GenerateContentConfig(**kwargs)

    def _extract_image(
        self, resp: Any, *, prompt: str, size: str,
        source: str, model: str,
    ) -> GeneratedImage:
        """Pull inline image bytes from a SDK response, upload to S3, wrap."""
        for cand in (resp.candidates or []):
            content = getattr(cand, "content", None)
            if content is None:
                continue
            for part in (content.parts or []):
                inline = getattr(part, "inline_data", None)
                if inline is None:
                    continue
                raw = getattr(inline, "data", None)
                if not raw:
                    continue
                mime = getattr(inline, "mime_type", None) or "image/jpeg"
                ext = ".png" if "png" in mime else ".jpg"
                # SDK may return bytes directly or base64 string depending
                # on version — handle both.
                if isinstance(raw, str):
                    raw = base64.b64decode(raw)
                url = self._upload_bytes(raw, ext=ext)
                # Billed here, not at the call site: this is the one funnel
                # both generate_sync and generate_async pass through, and it
                # sees the model/size that ACTUALLY rendered after any
                # fallback or 2K→1K downgrade — the pair the price depends on.
                costs.record_gemini_image(
                    model, size, getattr(resp, "usage_metadata", None))
                return GeneratedImage.from_url(
                    url=url, prompt=prompt, size=size, source=source,
                )
        raise RuntimeError(
            f"Nano Banana returned no image candidates "
            f"(model={model}, candidates={len(resp.candidates or [])})"
        )

    def _upload_bytes(self, raw: bytes, *, ext: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(raw)
            tmp_path = tf.name
        try:
            return self._uploader.upload(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def make_image_client(settings: Settings,
                      uploader: S3Uploader) -> NanoBananaImageGenerator:
    return NanoBananaImageGenerator(settings, uploader)
