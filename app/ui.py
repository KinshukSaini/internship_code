"""Gradio Blocks UI for video generation."""
from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr

from app.chain import ChainFailure, ChunkPlan, run_chain
from app.costs import format_markdown as format_cost_markdown
from app.config import (
    BROLL_VIDEO_PROVIDER,
    BURN_CAPTIONS,
    CHUNK_SECONDS,
    COLOR_MATCH_CLIPS,
    DEFAULT_CLIP_RESOLUTION,
    DEFAULT_LIPSYNC_ENGINE,
    DEFAULT_LLM_PROVIDER,
    FINISHING_GRADE,
    FINISHING_LOOK,
    PUNCH_IN_ACCENT,
    ROOM_TONE,
    SYNC_LIPSYNC_GRANULARITY,
    TRANSITION_STYLE,
    VIDEO_GENERATOR,
    VISUAL_QC,
    VOICE_PROVIDER,
    load_settings,
)
from app.gallery import Gallery
from app.gemini import (
    CharacterDescription,
    ChunkSpec,
    GeminiClient,
    ScriptDraft,
)
from app.image_gen import make_image_client
from app.jobs import JobLog
from app.pipeline import (
    WORKFLOWS_ROOT,
    generate_ugc_video,
    resumable_runs,
    resume_plan,
    resume_ugc_video,
)
from app.stitcher import (
    download as ffmpeg_download,
    has_ffmpeg,
    stitch as ffmpeg_stitch,
)
from app.uploader import make_uploader
from app.validation import (
    Asset,
    Settings as FormSettings,
    ValidationError,
    validate_form,
)
from app.video_gen import (
    DEFAULT_MODEL_ID,
    FAST_MODEL_ID,
    VideoRequest,
    make_video_client,
)
from app.workflow import WorkflowSession, save_session

MAX_IMAGE_SLOTS = 2   # EvoLink image-to-video FLF2V accepts 1-2 (first + last)
MAX_WORKFLOW_CHUNKS = 8  # ceil(60s max / 8s per chunk)

IMAGE_ROLES = ["first_frame", "last_frame"]

MODELS = [DEFAULT_MODEL_ID, FAST_MODEL_ID]
RATIOS = ["9:16", "16:9", "1:1", "4:3", "3:4", "21:9", "adaptive"]
RESOLUTIONS = ["480p", "720p", "1080p"]


def _is_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith(("http://", "https://", "asset://"))


def _log(buf: str, msg: str) -> str:
    """Append a timestamped line to the log buffer AND echo to stderr."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    return f"{buf}{line}\n" if buf else f"{line}\n"


def _file_path(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return getattr(value, "name", None)


def _resolve_slot(file_value, url_value: str) -> str | None:
    url = (url_value or "").strip()
    if url:
        return url
    return _file_path(file_value)


def _collect_image_assets(
    image_files, image_urls, image_roles, n_imgs,
) -> list[Asset]:
    assets: list[Asset] = []
    for i in range(n_imgs):
        src = _resolve_slot(image_files[i], image_urls[i])
        if src:
            assets.append(Asset(local_path=src, kind="image", role=image_roles[i]))
    return assets


def _run_generation(
    prompt: str,
    *args,
    progress: gr.Progress = gr.Progress(),
):
    n_img = MAX_IMAGE_SLOTS

    idx = 0
    image_files_raw = list(args[idx:idx + n_img]); idx += n_img
    image_urls = list(args[idx:idx + n_img]); idx += n_img
    image_roles = list(args[idx:idx + n_img]); idx += n_img
    n_visible_imgs = int(args[idx]); idx += 1

    model = args[idx]; idx += 1
    ratio = args[idx]; idx += 1
    duration = int(args[idx]); idx += 1
    resolution = args[idx]; idx += 1
    generate_audio = bool(args[idx]); idx += 1
    return_last_frame = bool(args[idx]); idx += 1
    seed_str = (args[idx] or "").strip(); idx += 1

    seed = int(seed_str) if seed_str else None
    settings = FormSettings(
        model=model, ratio=ratio, duration=duration,
        resolution=resolution, generate_audio=generate_audio,
        return_last_frame=return_last_frame, seed=seed,
    )

    assets = _collect_image_assets(
        image_files_raw, image_urls, image_roles, n_visible_imgs,
    )

    log_buf = ""
    log_buf = _log(log_buf, f"new request · {len(assets)} image asset(s)")
    yield log_buf, None, ""

    try:
        validate_form(prompt=prompt, assets=assets, settings=settings)
    except ValidationError as e:
        log_buf = _log(log_buf, f"VALIDATION ERROR: {e}")
        yield log_buf, None, ""
        return

    try:
        app_settings = load_settings()
    except RuntimeError as e:
        log_buf = _log(log_buf, f"CONFIG ERROR: {e}")
        yield log_buf, None, ""
        return

    needs_uploader = any(not _is_url(a.local_path) for a in assets)
    uploader = None
    if needs_uploader:
        try:
            uploader = make_uploader(app_settings)
        except RuntimeError as e:
            log_buf = _log(log_buf, f"UPLOAD SETUP ERROR: {e}")
            yield log_buf, None, ""
            return

    client = make_video_client(app_settings)
    job_log = JobLog()

    log_buf = _log(log_buf, "resolving images...")
    yield log_buf, None, ""

    # Resolve every asset to a public URL, keyed by role.
    by_role: dict[str, str] = {}
    for i, a in enumerate(assets):
        progress((i + 1) / max(len(assets) + 1, 1),
                 desc=f"Image {i + 1}/{len(assets)}")
        if _is_url(a.local_path):
            url = a.local_path
            log_buf = _log(
                log_buf,
                f"image {i + 1}/{len(assets)} [{a.role}] using URL: "
                f"{url[:80]}{'...' if len(url) > 80 else ''}",
            )
        else:
            log_buf = _log(
                log_buf,
                f"image {i + 1}/{len(assets)} [{a.role}] uploading to S3...",
            )
            yield log_buf, None, ""
            try:
                url = uploader.upload(a.local_path)
            except Exception as e:
                log_buf = _log(log_buf, f"UPLOAD FAILED: {e}")
                yield log_buf, None, ""
                return
            log_buf = _log(log_buf, f"  -> {url}")
        by_role[a.role] = url
        yield log_buf, None, ""

    first_frame_url = by_role.get("first_frame")
    if not first_frame_url:
        log_buf = _log(log_buf, "VALIDATION ERROR: at least one image with role 'first_frame' is required for image-to-video.")
        yield log_buf, None, ""
        return
    request = VideoRequest(
        prompt=prompt,
        first_frame_url=first_frame_url,
        last_frame_url=by_role.get("last_frame"),
        duration=duration,
        aspect_ratio=ratio,
        resolution=resolution,
        generate_audio=generate_audio,
        seed=seed,
        model=model,
    )

    log_buf = _log(log_buf, "creating EvoLink Seedance task...")
    yield log_buf, None, ""
    progress(0.1, desc="Creating task")

    try:
        task_id, payload = client.create_task(request)
    except Exception as e:
        log_buf = _log(log_buf, f"TASK CREATE FAILED: {e}")
        yield log_buf, None, ""
        return

    job_log.write_initial(
        task_id=task_id,
        prompt=prompt,
        assets=[{"url": v, "kind": "image", "role": k} for k, v in by_role.items()],
        settings=vars(settings),
        request_payload=payload,
    )
    log_buf = _log(log_buf, f"task created: {task_id}")
    log_buf = _log(log_buf, "polling every 10s...")
    yield log_buf, None, ""

    final = None
    poll_n = 0
    try:
        for status, resp in client.poll_task_stream(task_id):
            poll_n += 1
            log_buf = _log(log_buf, f"poll #{poll_n}: status={status}")
            job_log.append_status(task_id, status)
            if resp is not None:
                final = resp
            yield log_buf, None, ""
    except RuntimeError as e:
        job_log.append_status(task_id, "failed")
        log_buf = _log(log_buf, f"GENERATION FAILED: {e}")
        yield log_buf, None, ""
        return

    video_url = final.video_url if final else None
    result_dict = final.raw if final else {}
    job_log.write_final(task_id, result_dict)

    log_buf = _log(log_buf, f"DONE · video_url={video_url}")
    pretty = json.dumps(result_dict, indent=2, default=str)
    yield log_buf, video_url, pretty


# ===================================================================
# Tab 2 — Direct video (existing v1 UI, now nested under a tab)
# ===================================================================

def _build_direct_video_tab() -> None:
    gr.Markdown(
        "**EvoLink Seedance 2.0 image-to-video (FLF2V).** Provide one "
        "image (role: `first_frame`) for an animation, or two images "
        "(`first_frame` + `last_frame`) for a pinned-endpoint transition."
    )

    with gr.Row():
        with gr.Column(scale=1):
            prompt = gr.Textbox(
                label="Prompt", lines=4,
                placeholder="Describe the motion / dialogue / scene...",
            )

            gr.Markdown("### Images (1 or 2)")
            image_files: list[Any] = []
            image_urls: list[Any] = []
            image_roles: list[Any] = []
            image_rows: list[Any] = []
            default_roles = ["first_frame", "last_frame"]
            for i in range(MAX_IMAGE_SLOTS):
                with gr.Row(visible=(i == 0)) as row:
                    f = gr.File(label=f"Image {i + 1}", file_types=["image"])
                    u = gr.Textbox(
                        label="...or URL / asset://id",
                        placeholder="https://... or asset://asset-...",
                        scale=2,
                    )
                    r = gr.Dropdown(IMAGE_ROLES, value=default_roles[i],
                                    label="role", interactive=True)
                image_files.append(f); image_urls.append(u)
                image_roles.append(r); image_rows.append(row)
            n_visible_imgs = gr.State(1)
            add_image_btn = gr.Button("+ Add image", size="sm")

            gr.Markdown("### Settings")
            model = gr.Dropdown(MODELS, value=MODELS[0], label="Model")
            ratio = gr.Dropdown(RATIOS, value="9:16", label="Aspect ratio")
            duration = gr.Slider(4, 15, value=8, step=1, label="Duration (s)")
            resolution = gr.Dropdown(RESOLUTIONS, value="720p", label="Resolution")
            generate_audio = gr.Checkbox(value=True, label="generate_audio")
            return_last_frame = gr.Checkbox(
                value=False,
                label="return_last_frame (EvoLink ignores; UI compat only)",
            )
            seed = gr.Textbox(value="", label="seed (blank = random)")

            generate_btn = gr.Button("Generate", variant="primary")

        with gr.Column(scale=1):
            status = gr.Textbox(
                label="Log", value="", lines=18, max_lines=40,
                interactive=False, autoscroll=True,
            )
            video_out = gr.Video(label="Result", interactive=False)
            with gr.Accordion("Job details", open=False):
                job_json = gr.Code(language="json", label="Full job response")

    def _make_reveal(max_n: int, rows):
        def handler(current: int):
            new = min(current + 1, max_n)
            row_updates = [gr.update(visible=(i < new)) for i in range(max_n)]
            return [new, *row_updates]
        return handler

    add_image_btn.click(
        _make_reveal(MAX_IMAGE_SLOTS, image_rows),
        inputs=[n_visible_imgs],
        outputs=[n_visible_imgs, *image_rows],
    )

    def _clamp_resolution(model_name: str, current: str):
        # EvoLink's fast variant caps at 720p.
        if "-fast-" in model_name:
            choices = ["480p", "720p"]
            value = "720p" if current == "1080p" else current
        else:
            choices = RESOLUTIONS
            value = current
        return gr.update(choices=choices, value=value)

    model.change(_clamp_resolution,
                 inputs=[model, resolution], outputs=resolution)

    generate_btn.click(
        _run_generation,
        inputs=[
            prompt,
            *image_files, *image_urls, *image_roles, n_visible_imgs,
            model, ratio, duration, resolution,
            generate_audio, return_last_frame, seed,
        ],
        outputs=[status, video_out, job_json],
    )


# ===================================================================
# Tab 3 — Nano Banana gallery
# ===================================================================

def _build_gallery_tab() -> None:
    gallery_store = Gallery()

    with gr.Row():
        with gr.Column(scale=1):
            prompt = gr.Textbox(label="Prompt", lines=5,
                                placeholder="Describe the image you want...")
            tier = gr.Radio(
                choices=[
                    ("Hero (Nano Banana Pro) — photoreal, slow, ~4× cost", "hero"),
                    ("Anchor (Nano Banana 2, thinking=high) — creator content, fast", "anchor"),
                ],
                value="anchor",
                label="Tier",
            )
            size = gr.Dropdown(["1K", "2K", "4K"], value="2K", label="Size")
            aspect = gr.Dropdown(
                ["9:16", "16:9", "1:1", "4:5", "3:4", "4:3", "5:4", "2:3", "3:2", "21:9"],
                value="9:16", label="Aspect ratio",
            )
            n = gr.Slider(1, 4, value=1, step=1, label="How many")
            source_filter = gr.Dropdown(
                ["all", "tab3", "workflow"], value="all",
                label="Show in gallery"
            )
            gen_btn = gr.Button("Generate", variant="primary")
            log = gr.Textbox(label="Log", lines=6, interactive=False,
                             autoscroll=True)
        with gr.Column(scale=2):
            current = gr.Image(label="Most recent", interactive=False)
            current_url = gr.Textbox(label="S3 URL", interactive=False)
            current_prompt = gr.Textbox(label="Prompt used",
                                        interactive=False, lines=3)
            grid = gr.Gallery(label="All generated images",
                              columns=4, height=480, allow_preview=True)

    def _refresh_grid(source: str):
        rows = gallery_store.list(
            source=None if source == "all" else source,
        )
        return [(row.url, row.prompt[:80]) for row in rows]

    def _generate(prompt_str: str, tier_str: str, size_str: str,
                  aspect_str: str, n_int: float, source_str: str):
        try:
            settings = load_settings()
            uploader = make_uploader(settings)
        except RuntimeError as e:
            yield "", "", "", _refresh_grid(source_str), f"CONFIG ERROR: {e}"
            return
        client = make_image_client(settings, uploader)
        yield "", "", "", _refresh_grid(source_str), f"calling Nano Banana ({tier_str})..."
        try:
            results = client.generate_sync(
                prompt=prompt_str, tier=tier_str, size=size_str,
                aspect_ratio=aspect_str, n=int(n_int), source="tab3",
            )
        except Exception as e:
            yield "", "", "", _refresh_grid(source_str), f"ERROR: {e}"
            return
        for r in results:
            gallery_store.add(r)
        latest = results[-1]
        yield (
            latest.url, latest.url, latest.prompt,
            _refresh_grid(source_str),
            f"generated {len(results)} image(s)",
        )

    gen_btn.click(
        _generate,
        inputs=[prompt, tier, size, aspect, n, source_filter],
        outputs=[current, current_url, current_prompt, grid, log],
    )
    source_filter.change(
        lambda s: _refresh_grid(s),
        inputs=[source_filter], outputs=[grid],
    )
    grid.value = _refresh_grid("all")


# ===================================================================
# Tab 1 — Full Workflow wizard
# ===================================================================

def _pronouns(gender: str) -> dict[str, str]:
    g = (gender or "").lower().strip()
    if g.startswith("m"):
        return {"subj": "he", "obj": "him", "poss": "his"}
    if g.startswith("f"):
        return {"subj": "she", "obj": "her", "poss": "her"}
    return {"subj": "they", "obj": "them", "poss": "their"}


def _char_image_prompt(c) -> str:
    """Nano Banana prompt for the character reference image.

    Tuned for Indian UGC creators (male and female) across the full range of
    Indian skin tones (fair to deep brown). The template never prescribes a
    skin tone — it trusts c.face from Gemini to pick the right one for the
    brief. Imperfection cues are Indian-neutral ("subtle natural skin
    texture, occasional small natural mole") — freckles/peach-fuzz removed
    as Western-coded.

    Prompt-engineering structure: Subject → Pose → Lighting → Camera/Lens/Film
    → Setting → Style → Negatives (Nano Banana weights earlier tokens more).
    Camera + lens + film stock act as rendering instructions, not flavor.
    Hands anchored by position, not counted (counting backfires on diffusion).
    Target ~140 words (within the safe 60–145 portrait-realism band).
    """
    p = _pronouns(c.gender)
    return (
        f"A {c.age}-year-old {c.ethnicity} Instagram content creator. "
        f"{c.face}. Natural eyes with a confident direct gaze, "
        f"fresh-faced look with light or no makeup, {c.hair}. Real "
        f"non-airbrushed skin with fine natural texture and subtle pores — "
        f"clear, healthy, {'dewy' if (c.gender or '').lower().startswith('f') else 'fresh matte (non-oily, non-shiny)'} and attractive influencer skin, not "
        f"airbrushed-plastic or over-smoothed, no moles or freckles added. "
        f"Wearing {c.wardrobe}. {c.energy}. Standing in a confident relaxed "
        f"three-quarter pose, hands natural and visible. Bright soft natural "
        f"daylight pouring from a large window camera-left — aspirational "
        f"modern Indian apartment morning light. Shot on Fujifilm X-T5, "
        f"50mm f/1.8, shallow depth of field, natural color rendering with "
        f"warm undertones, subtle natural film grain. Softly out-of-focus "
        f"modern Indian urban apartment interior background with natural "
        f"creator-life elements (plants, sunlight, curated décor). Modern "
        f"Indian Instagram lifestyle creator aesthetic, aspirational but "
        f"approachable, the kind of real creator with 50k–500k followers. "
        f"Vertical 9:16, waist-up framing, sharp focus on the eyes with "
        f"natural catchlights. No plastic-CGI texture, no waxy face, no "
        f"over-smoothed skin, no beauty filter, no AI face, no stock-photo "
        f"energy, no extra fingers, no distorted hands."
    )


def _first_frame_prompt(c, chunk, framing, product_ref_url,
                        start_with_product: bool = False) -> str:
    """Nano Banana prompt for the opening-frame composition.

    Same recipe as _char_image_prompt — no skin-tone prescription, Indian-
    neutral imperfection cues, gender-aware pronouns — plus a framing-
    specific composition + camera string and the chunk's opening action.

    start_with_product: if True AND a product reference exists, the first
    frame is composed with the product visible. If False (default), the
    first frame shows the creator without the product — the product still
    attaches as a separate reference_image so it can appear in the scene
    later in the chunk's action.
    """
    p = _pronouns(c.gender)

    if framing == "selfie":
        framing_desc = (
            "tight handheld arm-extended phone-selfie medium close-up, slight "
            "natural micro-shake, eye-level to slightly above eye-level"
        )
        camera_clause = (
            "Shot on iPhone 15 Pro front camera with slight natural lens "
            "distortion, warm Kodak Portra-like color, subtle natural grain"
        )
    elif framing == "tripod":
        framing_desc = (
            "static medium shot at eye level from a tripod, hands-free, the "
            "creator slightly off-center in frame"
        )
        camera_clause = (
            "Shot on Fujifilm X-T5, 50mm f/1.8, shallow depth of field, "
            "Kodak Portra 400 tones, subtle natural film grain"
        )
    elif framing == "counter":
        framing_desc = (
            "behind-the-counter static medium shot looking slightly down at "
            f"{p['obj']}, the counter surface taking the bottom third of "
            f"frame"
        )
        camera_clause = (
            "Shot on Fujifilm X-T5, 35mm f/2, modest depth of field, Kodak "
            "Portra 400 tones, subtle natural film grain"
        )
    else:  # walking
        framing_desc = (
            "walking-and-talking handheld medium shot, slight natural sway, "
            "the creator moving through a soft-focus interior space"
        )
        camera_clause = (
            "Shot on Fujifilm X-T5, 35mm f/2, shallow depth of field, Kodak "
            "Portra 400 tones, subtle natural film grain"
        )

    if product_ref_url and start_with_product:
        product_clause = (
            f" The product (visible at {product_ref_url}) is in the opening "
            f"frame, held naturally or set on the surface as part of the "
            f"opening composition."
        )
    else:
        # Product reference is still attached to the chunk as a
        # reference_image, but the opening frame does NOT feature it — the
        # creator starts WITHOUT the product in hand.
        product_clause = ""

    return (
        f"A {c.age}-year-old {c.ethnicity} Instagram content creator. "
        f"{c.face}. Natural eyes with a confident relaxed gaze, fresh-faced "
        f"look with light or no makeup, {c.hair}. Real non-airbrushed skin "
        f"with fine natural texture and subtle pores — clear, healthy, "
        f"{'dewy' if (c.gender or '').lower().startswith('f') else 'fresh matte (non-oily, non-shiny)'} "
        f"and attractive influencer skin, not airbrushed-plastic or "
        f"over-smoothed, no moles or freckles added. "
        f"Wearing {c.wardrobe}. {c.energy}. Opening visual action: "
        f"{chunk.action}. One hand naturally engaged with the action, the "
        f"other relaxed.{product_clause} Bright soft natural daylight from "
        f"a window camera-left, single source, modern Indian creator morning "
        f"light. {framing_desc}. {camera_clause}. Modern Indian Instagram "
        f"lifestyle creator aesthetic, aspirational but approachable — the "
        f"kind of creator with 50k–500k followers, not a stock model. "
        f"Vertical 9:16, slightly off-center natural framing, sharp focus on "
        f"the eyes with natural catchlights, background in soft focus. No "
        f"plastic-CGI texture, no waxy face, no over-smoothed skin, no beauty "
        f"filter, no AI face, no stock-photo energy, "
        f"no extra fingers, no distorted hands."
    )


def _videos_md(results, stitched_path: str | None = None) -> str:
    lines = [f"### {len(results)} chunk(s) generated"]
    for r in results:
        lines.append(f"- **Chunk {r.index + 1}** — [open mp4]({r.video_url})")
    if stitched_path:
        lines.append(f"\n### Stitched final\n`{stitched_path}`")
    return "\n".join(lines)


def _log_append(buf: str, line: str) -> str:
    ts = datetime.now().strftime("%H:%M:%S")
    out = (buf or "") + f"[{ts}] {line}\n"
    print(f"[{ts}] {line}", file=sys.stderr, flush=True)
    return out


def _build_workflow_tab() -> None:
    gallery_store = Gallery()
    session_state = gr.State(None)

    gr.Markdown(
        "### Full Workflow (Brief → Script → Assets → Videos)\n"
        "Two approval gates: edit the draft script, then review the asset "
        "bundle. Chunks generate sequentially with character + last-frame "
        "continuity; all chunks auto-stitch into one mp4 at the end."
    )

    # ---------- Step 1: Brief ----------
    with gr.Group():
        gr.Markdown("**Step 1 · Brief**")
        brief = gr.Textbox(
            label="Brief", lines=4,
            placeholder="e.g., 15s UGC ad for Goat Life Oats...",
        )
        duration = gr.Slider(4, 60, value=15, step=1, label="Duration (s)")
        with gr.Row():
            product_file = gr.File(
                label="Product reference image (optional · upload)",
                file_types=["image"],
            )
            product_url = gr.Textbox(
                label="...or paste URL / asset://id",
                placeholder="https://... or asset://asset-...",
                scale=2,
            )
        with gr.Row():
            product_in_first = gr.Radio(
                choices=["Auto (let the system decide)",
                         "Force ON (product visible in first frame)",
                         "Force OFF (start without product on screen)"],
                value="Auto (let the system decide)",
                label="Product in first frame",
            )
            use_continuity = gr.Checkbox(
                value=True,
                label="Use previous chunk's video as motion reference",
            )
        draft_btn = gr.Button("Draft script", variant="primary")
        log = gr.Textbox(
            label="Log", lines=10, interactive=False, autoscroll=True,
        )

    # ---------- Step 2: Script review ----------
    # Panels are always visible — toggling Group visibility while emitting
    # values to its child components in the same return tuple has caused
    # Gradio 6.x to swallow the value updates. Empty panels are harmless.
    with gr.Group() as script_panel:
        gr.Markdown("**Step 2 · Script review** (edit if needed)")
        script_box = gr.Textbox(label="Script", lines=8)
        tone_box = gr.Textbox(label="Tone notes", lines=2)
        chunks_count = gr.Number(label="Chunks (auto)", interactive=False)
        approve_script_btn = gr.Button(
            "Approve & generate assets", variant="primary",
        )

    # ---------- Step 3: Asset bundle review ----------
    with gr.Group() as assets_panel:
        gr.Markdown(
            "**Step 3 · Asset bundle review**\n\n"
            "*Character reference* (left) is the identity anchor (NB Pro, "
            "neutral pose) — passed as a reference image to every anchor "
            "frame generation to lock identity. *Anchor frames* (right) "
            "are the N+1 chunk boundaries used by FLF2V Seedance."
        )
        with gr.Row():
            char_img = gr.Image(
                label="Character reference (locked identity)",
                interactive=False,
                height=400,
                scale=1,
            )
            anchors_gallery = gr.Gallery(
                label="Chunk boundary anchors (N+1 frames)",
                columns=3, height=400, allow_preview=True,
                scale=2,
            )
        prompts_box = gr.Textbox(
            label="Seedance prompts (one per chunk, separated by `=== CHUNK N ===`)",
            lines=20, max_lines=40,
        )
        approve_assets_btn = gr.Button(
            "Approve & generate videos", variant="primary",
        )

    # ---------- Step 4: Results ----------
    with gr.Group() as result_panel:
        gr.Markdown("**Step 4 · Generated videos**")
        videos_md = gr.Markdown("No videos yet.")
        with gr.Row():
            chunk_videos = [
                gr.Video(label=f"Chunk {i + 1}", interactive=False,
                         visible=False)
                for i in range(MAX_WORKFLOW_CHUNKS)
            ]
        stitched_video = gr.Video(
            label="Stitched final video", interactive=False, visible=False,
        )
        videos_json = gr.Code(label="Chunk results JSON", language="json")

    def _resolve_product_in_first(radio_value: str) -> str:
        """Map radio label → mode string stored in session."""
        if "ON" in radio_value:
            return "force_on"
        if "OFF" in radio_value:
            return "force_off"
        return "auto"

    def _empty_result_outputs():
        """Tuple of outputs for the result panel when nothing has rendered.
        Order MUST match the approve_assets_btn outputs list below."""
        chunk_updates = [gr.update(value=None, visible=False)
                         for _ in range(MAX_WORKFLOW_CHUNKS)]
        stitched_update = gr.update(value=None, visible=False)
        return (
            "No videos yet.",
            *chunk_updates,
            stitched_update,
            "",
            gr.update(),  # panel always visible — no-op update
        )

    def _result_outputs(results, chunk_local_paths, stitched_path):
        """Build the result-panel output tuple from a list of ChunkResults."""
        chunk_updates = []
        for i in range(MAX_WORKFLOW_CHUNKS):
            if i < len(results):
                # Prefer local file (renders reliably in gr.Video); fall back
                # to remote URL if download failed for this chunk.
                video_src = (
                    chunk_local_paths[i] if i < len(chunk_local_paths)
                    and chunk_local_paths[i]
                    else results[i].video_url
                )
                chunk_updates.append(
                    gr.update(value=video_src, visible=True)
                )
            else:
                chunk_updates.append(gr.update(value=None, visible=False))
        stitched_update = (
            gr.update(value=stitched_path, visible=True)
            if stitched_path else gr.update(value=None, visible=False)
        )
        results_json = json.dumps(
            [{"index": r.index, "video_url": r.video_url,
              "last_frame_url": r.last_frame_url, "task_id": r.task_id}
             for r in results],
            indent=2,
        )
        return (
            _videos_md(results, stitched_path),
            *chunk_updates,
            stitched_update,
            results_json,
            gr.update(),  # panel always visible — no-op update
        )

    # ---------- on_draft ----------
    async def on_draft(brief_str, duration_int, product_file_val, product_str,
                       product_first_radio, use_cont_bool, sess):
        if not brief_str.strip():
            return ("", "", 0, gr.update(visible=False),
                    sess, "ERROR: brief is required")
        try:
            settings = load_settings()
        except RuntimeError as e:
            return ("", "", 0, gr.update(visible=False), sess,
                    f"CONFIG ERROR: {e}")

        log_buf = _log_append("", "creating workflow session...")

        # Resolve product reference: URL wins over file. If only a file was
        # provided, upload it to S3 now so downstream calls can hand EvoLink
        # a public URL.
        product_resolved = (product_str or "").strip() or None
        if not product_resolved and product_file_val is not None:
            local = _file_path(product_file_val)
            if local:
                log_buf = _log_append(
                    log_buf, f"uploading product image to S3: {local}",
                )
                try:
                    up = make_uploader(settings)
                    product_resolved = up.upload(local)
                    log_buf = _log_append(log_buf, f"  -> {product_resolved}")
                except Exception as e:
                    return ("", "", 0, gr.update(visible=False),
                            sess,
                            _log_append(log_buf,
                                        f"PRODUCT UPLOAD FAILED: {e}"))

        sess_obj = WorkflowSession.new(
            brief=brief_str, duration_s=int(duration_int),
            product_ref_url=product_resolved,
        )
        # v2.1 controls — store the user's mode now; the resolved boolean
        # for "auto" mode is decided later in on_approve_script via Gemini
        # (after we have chunks[0].action).
        sess_obj.use_video_continuity = bool(use_cont_bool)
        sess_obj.start_with_product_mode = _resolve_product_in_first(
            product_first_radio
        )
        sess_obj.start_with_product = (
            sess_obj.start_with_product_mode == "force_on"
        )
        save_session(sess_obj)

        gem = GeminiClient(
            settings,
            skill_md_path=Path("video-seedance-skill.md"),
            image_skill_md_path=Path("docs/character-image-skill.md"),
        )
        log_buf = _log_append(log_buf, f"workflow {sess_obj.id} created")
        log_buf = _log_append(log_buf, "calling Gemini to draft script...")

        try:
            draft = await gem.generate_script(
                brief=brief_str, duration_s=int(duration_int),
            )
        except Exception as e:
            return ("", "", 0, gr.update(visible=False),
                    sess_obj.to_dict(),
                    _log_append(log_buf, f"GEMINI ERROR: {e}"))

        sess_obj.script = draft.model_dump()
        sess_obj.advance_to("script_review")
        save_session(sess_obj)
        log_buf = _log_append(
            log_buf,
            f"script drafted ({len(draft.full_script.split())} words, "
            f"{draft.n_chunks} chunks)",
        )
        sess_dict_out = sess_obj.to_dict()
        log_buf = _log_append(log_buf, "on_draft: about to return tuple to Gradio")
        return (
            draft.full_script, draft.tone_notes, draft.n_chunks,
            gr.update(),  # panel always visible — no-op update
            sess_dict_out, log_buf,
        )

    draft_btn.click(
        on_draft,
        inputs=[brief, duration, product_file, product_url,
                product_in_first, use_continuity, session_state],
        outputs=[script_box, tone_box, chunks_count,
                 script_panel, session_state, log],
    )

    # ---------- on_approve_script ----------
    async def on_approve_script(script_str, tone_str, n_chunks_int,
                                sess_dict, log_buf):
        if not sess_dict:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, "ERROR: no session"))
        settings = load_settings()
        gem = GeminiClient(
            settings,
            skill_md_path=Path("video-seedance-skill.md"),
            image_skill_md_path=Path("docs/character-image-skill.md"),
        )
        # Nano Banana returns base64; image client uploads to S3 to give us
        # public URLs for EvoLink Seedance. Reuses the same content-addressable
        # uploader as user-uploaded assets.
        try:
            uploader = make_uploader(settings)
        except RuntimeError as e:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, f"UPLOAD SETUP ERROR: {e}"))
        seedr = make_image_client(settings, uploader)

        sess_dict["script"] = {
            "full_script": script_str,
            "tone_notes": tone_str,
            "n_chunks": int(n_chunks_int),
        }
        sess_dict["state"] = "generating_assets"
        log_buf = _log_append(log_buf, "running parallel Gemini calls...")

        draft = ScriptDraft.model_validate(sess_dict["script"])

        # Step A: parallel — draft the screenplay AND describe the character.
        try:
            screenplay, character = await asyncio.gather(
                gem.write_screenplay(brief=sess_dict["brief"], script=draft),
                gem.describe_character(brief=sess_dict["brief"]),
            )
        except Exception as e:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, f"GEMINI ERROR: {e}"))

        sess_dict["screenplay"] = screenplay.model_dump()
        sess_dict["character"] = character.model_dump()
        beat_framings = [b.framing for b in screenplay.scene_beats]
        log_buf = _log_append(
            log_buf,
            f"screenplay drafted: framings per chunk = {beat_framings}",
        )

        # Step B: split the dialogue against the screenplay beats.
        try:
            chunks = await gem.split_script(
                brief=sess_dict["brief"], script=draft,
                screenplay=screenplay, n_chunks=draft.n_chunks,
            )
        except Exception as e:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, f"GEMINI ERROR: {e}"))

        sess_dict["chunks"] = [c.model_dump() for c in chunks]
        log_buf = _log_append(
            log_buf,
            f"split={len(chunks)} chunks; per-chunk framings = "
            f"{[c.framing for c in chunks]}",
        )

        # Resolve start_with_product: when mode is "auto" AND a product
        # ref was provided, ask Gemini whether the product belongs in the
        # opening frame.
        mode = sess_dict.get("start_with_product_mode", "auto")
        product_url = sess_dict.get("product_ref_url")
        if mode == "force_on":
            sess_dict["start_with_product"] = True
        elif mode == "force_off":
            sess_dict["start_with_product"] = False
        elif product_url:
            try:
                decision = await gem.should_open_with_product(
                    brief=sess_dict["brief"],
                    opening_chunk_action=chunks[0].action,
                )
                sess_dict["start_with_product"] = bool(decision)
                log_buf = _log_append(
                    log_buf,
                    f"Gemini auto-decision: start_with_product="
                    f"{decision}",
                )
            except Exception as e:
                log_buf = _log_append(
                    log_buf,
                    f"auto-decision failed ({e}); defaulting to False",
                )
                sess_dict["start_with_product"] = False
        else:
            # No product → first frame can't include one
            sess_dict["start_with_product"] = False

        log_buf = _log_append(
            log_buf,
            f"start_with_product resolved → {sess_dict['start_with_product']}",
        )

        # ───────────────────────────────────────────────────────────────
        # Phase 4 (Identity-locked FLF2V):
        #   Step A: write all text prompts in parallel (character ref +
        #           N+1 anchors + N seedance prompts).
        #   Step B: render the character reference image (NB Pro, hero
        #           tier). This is the IDENTITY ANCHOR — composed neutral
        #           portrait, used as a reference image input to every
        #           subsequent anchor generation.
        #   Step C: render N+1 anchor frames in parallel (NB2), each
        #           passing the character ref URL as a reference image.
        #           This locks identity at the SDK level — every anchor
        #           sees the same face/hair/wardrobe as input.
        # ───────────────────────────────────────────────────────────────
        n_chunks = len(chunks)
        try:
            log_buf = _log_append(
                log_buf,
                f"writing character ref + {n_chunks + 1} anchor + "
                f"{n_chunks} Seedance prompts in parallel via Gemini...",
            )
            anchor_prompt_specs: list[tuple[str, str]] = []
            for i, c in enumerate(chunks):
                anchor_prompt_specs.append((c.action, c.framing))
            last_chunk = chunks[-1]
            closing_action = (
                last_chunk.closing_action
                or f"final freeze on the last moment of: {last_chunk.action}"
            )
            anchor_prompt_specs.append((closing_action, last_chunk.framing))

            # prev_summary[i] = chunks[i-1].summary for i>0, else None.
            # ChunkSpec.summary is known here (produced by split_script).
            prev_summaries: list[str | None] = (
                [None] + [c.summary for c in chunks[:-1]]
            )

            # ─── Step A: all text gen in one parallel batch ────────────
            char_ref_prompt, anchor_prompts, seedance_prompts = await asyncio.gather(
                gem.write_character_prompt(
                    brief=sess_dict["brief"], character=character,
                ),
                asyncio.gather(*[
                    gem.write_first_frame_prompt(
                        brief=sess_dict["brief"], character=character,
                        opening_chunk_action=action,
                        framing=framing,
                        product_in_first_frame=sess_dict.get(
                            "start_with_product", False,
                        ),
                        product_ref_url=sess_dict.get("product_ref_url"),
                    )
                    for (action, framing) in anchor_prompt_specs
                ]),
                asyncio.gather(*[
                    gem.write_seedance_prompt(
                        chunk=c, character=character, prev_summary=ps,
                    )
                    for c, ps in zip(chunks, prev_summaries)
                ]),
            )
            log_buf = _log_append(
                log_buf,
                f"text prompts ready ({len(anchor_prompts)} anchor + "
                f"{len(seedance_prompts)} seedance + 1 char ref)",
            )

            # ─── Step B: render character ref (NB Pro, hero tier) ─────
            log_buf = _log_append(
                log_buf,
                "rendering canonical character reference via Nano Banana Pro...",
            )
            char_ref_images = await seedr.generate_async(
                prompt=char_ref_prompt, tier="hero", source="workflow",
            )
            character_ref = char_ref_images[0]
            log_buf = _log_append(
                log_buf,
                f"character reference ready → {character_ref.url[:60]}...",
            )

            # ─── Step C: render N+1 anchors in parallel, identity-locked
            # ref_urls[0] = character (identity lock).
            # ref_urls[1] = product (visual fidelity, when uploaded).
            ref_urls = [character_ref.url]
            product_url = sess_dict.get("product_ref_url")
            if product_url:
                ref_urls.append(product_url)
            log_buf = _log_append(
                log_buf,
                f"rendering {len(anchor_prompts)} anchor frames in parallel "
                f"via Nano Banana 2 ({len(ref_urls)} reference image"
                f"{'s' if len(ref_urls) > 1 else ''}: "
                f"character{' + product' if product_url else ''})...",
            )
            anchor_image_lists = await asyncio.gather(*[
                seedr.generate_async(
                    prompt=p, tier="anchor", source="workflow",
                    reference_image_urls=ref_urls,
                )
                for p in anchor_prompts
            ])
            anchor_images = [imgs[0] for imgs in anchor_image_lists]
            for i, img in enumerate(anchor_images):
                log_buf = _log_append(
                    log_buf,
                    f"  anchor {i + 1}/{len(anchor_images)} → "
                    f"{img.url[:60]}...",
                )
        except Exception as e:
            tb = traceback.format_exc()
            # Stderr already gets the full traceback via the print in _log_append;
            # the UI log shows the exception type + message so the user knows
            # what kind of failure occurred without scrolling through stack.
            log_buf = _log_append(
                log_buf,
                f"NANO BANANA ERROR: {type(e).__name__}: {e}",
            )
            print(tb, file=sys.stderr, flush=True)
            return (None, None, "", gr.update(visible=False),
                    sess_dict, log_buf)

        # Save to gallery: character ref + all anchors.
        gallery_store.add(character_ref)
        for img in anchor_images:
            gallery_store.add(img)

        # Persist on session for run_chain + later UI reads.
        sess_dict["anchor_urls"] = [a.url for a in anchor_images]
        sess_dict["character_ref"] = character_ref.to_dict()
        sess_dict["first_frame"] = anchor_images[0].to_dict()
        sess_dict["seedance_prompts"] = seedance_prompts
        prompts = seedance_prompts  # alias for the joined-output line below
        log_buf = _log_append(
            log_buf,
            f"{len(anchor_images)} anchors + 1 character ref generated",
        )
        sess_dict["state"] = "asset_review"
        sess_obj = WorkflowSession(
            **{k: v for k, v in sess_dict.items()
               if k in WorkflowSession.__dataclass_fields__}
        )
        save_session(sess_obj)

        joined = "\n\n".join(
            f"=== CHUNK {i} ===\n{p}" for i, p in enumerate(prompts)
        )
        log_buf = _log_append(
            log_buf,
            f"asset bundle ready ({len(prompts)} Seedance prompts, "
            f"{len(anchor_images)} FLF2V anchors, 1 character ref)",
        )
        # char_img = the locked-identity character reference (NB Pro, neutral pose).
        # anchors_gallery = all N+1 chunk boundary frames, in order.
        anchor_thumbnails = [
            (img.url, f"anchor {i} · " +
             ("opening" if i == 0
              else "closing" if i == len(anchor_images) - 1
              else f"chunk {i-1}→{i} boundary"))
            for i, img in enumerate(anchor_images)
        ]
        return (
            character_ref.url, anchor_thumbnails, joined,
            gr.update(),  # panel always visible — no-op update
            sess_dict, log_buf,
        )

    approve_script_btn.click(
        on_approve_script,
        inputs=[script_box, tone_box, chunks_count, session_state, log],
        outputs=[char_img, anchors_gallery, prompts_box,
                 assets_panel, session_state, log],
    )

    # ---------- on_approve_assets ----------
    async def on_approve_assets(prompts_text, sess_dict, log_buf):
        if not sess_dict:
            log_buf = _log_append(log_buf, "ERROR: no session")
            return (*_empty_result_outputs(), sess_dict, log_buf)

        # Parse the (possibly edited) prompts back out of the textbox.
        parts = [p.strip() for p in prompts_text.split("=== CHUNK ")
                 if p.strip()]
        prompts = []
        for part in parts:
            try:
                _, rest = part.split("===\n", 1)
            except ValueError:
                rest = part
            prompts.append(rest.strip())

        settings = load_settings()
        video_client = make_video_client(settings)
        log_buf = _log_append(
            log_buf, f"running EvoLink Seedance chain ({len(prompts)} chunks)...",
        )

        # FLF2V: each chunk uses two consecutive anchors as its first/last frame.
        anchor_urls = sess_dict.get("anchor_urls") or []
        if len(anchor_urls) != len(prompts) + 1:
            log_buf = _log_append(
                log_buf,
                f"ERROR: expected {len(prompts) + 1} anchor URLs "
                f"(N+1 for {len(prompts)} chunks), got {len(anchor_urls)}",
            )
            return (*_empty_result_outputs(), sess_dict, log_buf)

        # "Best config" defaults: 9:16 vertical UGC, 720p sweet-spot resolution,
        # 8s chunks (Seedance 2.0 quality plateau), audio on, no seed.
        plans = [
            ChunkPlan(
                seedance_prompt=prompts[i],
                first_frame_url=anchor_urls[i],
                last_frame_url=anchor_urls[i + 1],
                duration=CHUNK_SECONDS,
                aspect_ratio="9:16",
                resolution="720p",
                generate_audio=True,
                seed=None,
            )
            for i in range(len(prompts))
        ]

        progress_buf = {"v": log_buf}

        def on_progress(idx, msg):
            progress_buf["v"] = _log_append(
                progress_buf["v"], f"chunk {idx + 1}: {msg}",
            )

        try:
            results = await run_chain(
                client=video_client, chunks=plans,
                on_progress=on_progress,
            )
        except ChainFailure as e:
            progress_buf["v"] = _log_append(
                progress_buf["v"],
                f"FAILED at chunk {e.chunk_index + 1}: {e.underlying}",
            )
            partial = e.partial_results
            sess_dict["chunk_results"] = [
                {"index": r.index, "video_url": r.video_url,
                 "last_frame_url": r.last_frame_url, "task_id": r.task_id}
                for r in partial
            ]
            sess_dict["state"] = "failed"
            sess_dict["error"] = str(e.underlying)
            # Show whatever partial chunks we got; no stitching on failure.
            return (
                *_result_outputs(partial, [], None),
                sess_dict, progress_buf["v"],
            )

        # ---------- Download each chunk locally + stitch ----------
        chunk_local_paths: list[str] = []
        workflow_dir = WORKFLOWS_ROOT / sess_dict["id"]
        workflow_dir.mkdir(parents=True, exist_ok=True)
        progress_buf["v"] = _log_append(
            progress_buf["v"], "downloading chunk videos for stitching...",
        )
        for r in results:
            local = workflow_dir / f"chunk-{r.index}.mp4"
            try:
                ffmpeg_download(r.video_url, local)
                chunk_local_paths.append(str(local))
                progress_buf["v"] = _log_append(
                    progress_buf["v"],
                    f"  chunk {r.index + 1} -> {local}",
                )
            except Exception as e:
                progress_buf["v"] = _log_append(
                    progress_buf["v"],
                    f"  download failed for chunk {r.index + 1}: {e}",
                )
                chunk_local_paths.append("")
        sess_dict["chunk_local_paths"] = chunk_local_paths

        stitched_path: str | None = None
        downloaded = [Path(p) for p in chunk_local_paths if p]
        if len(downloaded) >= 2 and has_ffmpeg():
            stitched = workflow_dir / "stitched.mp4"
            try:
                ffmpeg_stitch(downloaded, stitched)
                stitched_path = str(stitched)
                sess_dict["stitched_video_path"] = stitched_path
                progress_buf["v"] = _log_append(
                    progress_buf["v"], f"stitched -> {stitched_path}",
                )
            except Exception as e:
                progress_buf["v"] = _log_append(
                    progress_buf["v"], f"STITCH ERROR: {e}",
                )
        elif len(downloaded) < 2:
            progress_buf["v"] = _log_append(
                progress_buf["v"],
                "skipping stitch — fewer than 2 chunks downloaded",
            )
        elif not has_ffmpeg():
            progress_buf["v"] = _log_append(
                progress_buf["v"],
                "ffmpeg not found; skipping stitch — individual chunks "
                "are still available",
            )

        sess_dict["chunk_results"] = [
            {"index": r.index, "video_url": r.video_url,
             "last_frame_url": r.last_frame_url, "task_id": r.task_id}
            for r in results
        ]
        sess_dict["state"] = "complete"
        # Persist the final session state.
        sess_obj = WorkflowSession(
            **{k: v for k, v in sess_dict.items()
               if k in WorkflowSession.__dataclass_fields__}
        )
        save_session(sess_obj)

        progress_buf["v"] = _log_append(
            progress_buf["v"], f"DONE · {len(results)} videos",
        )

        return (
            *_result_outputs(results, chunk_local_paths, stitched_path),
            sess_dict, progress_buf["v"],
        )

    approve_assets_btn.click(
        on_approve_assets,
        inputs=[prompts_box, session_state, log],
        outputs=[
            videos_md,
            *chunk_videos,
            stitched_video,
            videos_json,
            result_panel,
            session_state,
            log,
        ],
    )


# ===================================================================
# Workflow v2 — Phase-4 pipeline (VO-first, lipsync, B-roll, captions)
# ===================================================================

def _build_workflow_v2_tab() -> None:
    """One-shot caller of the headless app.pipeline.generate_ugc_video.

    Streams BOTH progress log AND every intermediate asset (script, character,
    voice, character-reference image, anchor frames, VO audio) as they're
    built — then the final lip-synced, captioned video.
    """
    gr.Markdown(
        "### Workflow v2 · Phase-4 pipeline\n"
        "Plan timeline → describe character (picks voice) → ElevenLabs VO → "
        "Nano Banana character ref + anchors → Seedance single-image I2V → "
        "lipsync (Sync.so post-hoc or Seedance native audio-driven) → "
        "assemble → captions. Assets stream into the panels below as "
        "they're built."
    )
    with gr.Row():
        # ── LEFT: inputs ──────────────────────────────────────────
        with gr.Column(scale=1):
            brief = gr.Textbox(
                label="Brief", lines=5,
                placeholder="e.g., UGC ad for a vitamin C serum...",
            )
            # Duration is decided by the AI (verbatim script → its natural
            # spoken length; rough idea → a self-chosen 15-45s). No slider.
            with gr.Row():
                hook_choice = gr.Dropdown(
                    ["auto", "pov", "stop_scrolling", "controversial",
                     "curiosity_gap", "question", "listicle", "result_first",
                     "callout", "myth_buster"],
                    value="auto", label="Hook type",
                )
                narrative_choice = gr.Dropdown(
                    ["auto", "pas", "storytime", "day_in_life", "testimonial",
                     "comparison", "listicle", "demo"],
                    value="auto", label="Narrative structure",
                )
            # Video generator: "seedance" is the default (EvoLink Seedance i2v
            # with TTS + lipsync); "omni" uses Gemini Omni which generates
            # speech natively from the prompt — no TTS, no cloning, no lipsync
            # pass, 720p ceiling.  Placed prominently so it immediately gates
            # the widgets below that are irrelevant in omni mode.
            video_generator = gr.Radio(
                choices=[
                    ("Seedance (TTS + lipsync)", "seedance"),
                    ("Omni — end-to-end, native voice (experimental)", "omni"),
                ],
                value=VIDEO_GENERATOR,
                label="Video generator",
                info=(
                    "Omni prompts the voice in words (no TTS or cloning) — "
                    "720p-only, no lipsync pass. Voice provider, voice source, "
                    "lipsync, and b-roll provider are ignored in Omni mode."
                ),
            )

            # Voice provider = which TTS engine speaks. The same real influencer
            # voices are cloned into all three, so this changes the engine, not
            # the voice identity. ElevenLabs alignment is the master clock on
            # every provider (see AlignedVoice), so its key is always required.
            voice_provider = gr.Radio(
                choices=[("ElevenLabs (eleven_v3 — native audio tags)",
                          "elevenlabs"),
                         ("Cartesia (sonic-3.5)", "cartesia"),
                         ("Fish Audio (s2.1-pro)", "fish")],
                value=VOICE_PROVIDER,
                label="Voice provider (TTS engine — timing always via "
                      "ElevenLabs forced alignment)",
            )

            # Voice source: default is auto-match a cloned voice to the cast
            # character; user may force a specific clone or the legacy catalog.
            # Choices are PROVIDER-SCOPED — only voices cloned on the selected
            # provider are offered, so a pick can never be unspeakable.
            from app.voice_library import load_voice_library as _load_vlib

            def _voice_src_choices_for(provider: str):
                vlib = _load_vlib(provider=provider)
                choices = [("Auto — match a cloned voice to the character",
                            "auto")]
                # The catalog holds ElevenLabs ids only — offering it on another
                # provider would hand it an id that provider cannot resolve.
                if provider == "elevenlabs":
                    choices.append(("Catalog (legacy vibe pick)", "catalog"))
                choices += [
                    (f"{vp.name} · {vp.gender}, {vp.age_band}, {vp.voice_vibe}",
                     f"clone:{vp.voice_id}") for vp in vlib
                ]
                return choices, len(vlib)

            def _voice_src_label(provider: str, n: int) -> str:
                return (f"Voice source ({n} cloned voices on {provider}) — "
                        "Auto picks the cloned voice that best matches the "
                        "character")

            _init_choices, _init_n = _voice_src_choices_for(VOICE_PROVIDER)
            voice_source = gr.Dropdown(
                choices=_init_choices, value="auto",
                label=_voice_src_label(VOICE_PROVIDER, _init_n),
            )
            voice_id = gr.Textbox(
                label="voice_id override for the selected provider (blank = use "
                      "Voice source above; a value here always wins)",
                placeholder="optional override",
            )

            def _on_voice_provider_change(provider: str):
                """Re-scope the Voice source dropdown to the new provider.

                Resets to "auto" because a clone:<id> value from the previous
                provider is meaningless to this one.
                """
                choices, n = _voice_src_choices_for(provider)
                return gr.update(choices=choices, value="auto",
                                 label=_voice_src_label(provider, n))

            voice_provider.change(
                _on_voice_provider_change,
                inputs=[voice_provider], outputs=[voice_source],
            )

            script_llm = gr.Radio(
                choices=[("Claude (Opus 4.8 + Sonnet 4.6)", "claude"),
                         ("Gemini", "gemini")],
                value=DEFAULT_LLM_PROVIDER,
                label="Script LLM",
            )
            with gr.Row():
                lipsync_engine = gr.Radio(
                    choices=[("Sync.so (recommended)", "syncso"),
                             ("HeyGen (cloud lipsync)", "heygen"),
                             ("HeyGen precision (Replicate) — isolated per-clip dub",
                              "heygen_replicate"),
                             ("Seedance native voice (timbre-anchored, "
                              "Indian-English)",
                              "seedance_native")],
                    value=DEFAULT_LIPSYNC_ENGINE,
                    label="Lipsync engine",
                )
                lipsync_granularity = gr.Radio(
                    choices=[("Per-clip (default)", "per_clip"),
                             ("Whole video (experimental — Sync.so only; "
                              "1 pass over the whole cut)", "whole")],
                    value=SYNC_LIPSYNC_GRANULARITY,
                    label="Lipsync granularity",
                )
                trim_silence = gr.Checkbox(
                    value=True, label="Trim VO silences",
                )
                clip_resolution = gr.Radio(
                    choices=[("720p (fast)", "720p"),
                             ("1080p (highest quality)", "1080p")],
                    value=DEFAULT_CLIP_RESOLUTION,
                    label="Clip resolution",
                )
                # Kling only ever takes the beats that are neither lip-synced
                # nor audio-driven (b-roll / reaction / silent / VO-over-b-roll)
                # — talking heads always stay on Seedance. 5.4 vs 13.5
                # credits/s, verified against our own EvoLink billing.
                broll_video_provider = gr.Radio(
                    choices=[("Seedance (all clips)", "seedance"),
                             ("Kling v3 (b-roll only — ~2.5× cheaper)",
                              "kling")],
                    value=BROLL_VIDEO_PROVIDER,
                    label="B-roll video provider",
                )

            # Omni ignores TTS and lipsync widgets — grey them out so the user
            # knows they have no effect.  Re-enable them when Seedance is chosen.
            # The outputs list must match the order used in the handler below.
            def _on_video_generator_change(generator: str):
                """Enable/disable widgets that Omni does not use."""
                enabled = generator != "omni"
                toggle = gr.update(interactive=enabled)
                return (
                    toggle,   # voice_provider
                    toggle,   # voice_source
                    toggle,   # voice_id
                    toggle,   # lipsync_engine
                    toggle,   # lipsync_granularity
                    toggle,   # clip_resolution
                    toggle,   # broll_video_provider
                )

            video_generator.change(
                _on_video_generator_change,
                inputs=[video_generator],
                outputs=[voice_provider, voice_source, voice_id,
                         lipsync_engine, lipsync_granularity,
                         clip_resolution, broll_video_provider],
            )
            with gr.Row():
                product_file = gr.File(
                    label="Product reference (optional)", file_types=["image"],
                )
                product_url = gr.Textbox(
                    label="...or product URL", scale=2,
                    placeholder="https://...",
                )
            with gr.Accordion("🎬 Finishing", open=False):
                with gr.Row():
                    burn_captions_cb = gr.Checkbox(
                        value=BURN_CAPTIONS,
                        label="Burn captions (word-pop, sound-off viewers)",
                    )
                    finishing_grade_cb = gr.Checkbox(
                        value=FINISHING_GRADE,
                        label="Phone-capture grade (contrast + grain)",
                    )
                    room_tone_cb = gr.Checkbox(
                        value=ROOM_TONE,
                        label="Room tone (no dead-silent gaps)",
                    )
                with gr.Row():
                    finishing_look_sel = gr.Radio(
                        choices=[("Clean bright (safe default)", "clean_bright"),
                                 ("Warm golden-hour", "warm_golden"),
                                 ("Teal-orange (punchy — use sparingly)",
                                  "teal_orange"),
                                 ("No look (grain only)", "none")],
                        value=FINISHING_LOOK,
                        label="Colour look",
                    )
                    color_match_cb = gr.Checkbox(
                        value=COLOR_MATCH_CLIPS,
                        label="Shot matching (match clips' exposure/cast "
                              "to the median)",
                    )
                with gr.Row():
                    transition_style_sel = gr.Radio(
                        choices=[("Zoom punch (UGC standard)", "punch"),
                                 ("Whip slide", "slide"),
                                 ("Plain hard cut", "cut")],
                        value=TRANSITION_STYLE,
                        label="Cut accent on b-roll (crossfades deliberately "
                              "not offered — they read corporate and desync "
                              "audio)",
                    )
                    punch_in_cb = gr.Checkbox(
                        value=PUNCH_IN_ACCENT,
                        label="Product-reveal punch-in (12% tighter framing)",
                    )
                    visual_qc_cb = gr.Checkbox(
                        value=VISUAL_QC,
                        label="AI review (Gemini defect audit, report-only)",
                    )
            gen_btn = gr.Button("Generate UGC video", variant="primary")

        # ── RIGHT: streaming progress + assets + final video ──────
        with gr.Column(scale=2):
            v2_log = gr.Textbox(label="Progress", lines=10, interactive=False,
                                autoscroll=True)
            v2_video = gr.Video(label="🎬 Final video", interactive=False)

            with gr.Accordion("📜 Script (from timeline planner)", open=True):
                v2_script = gr.Textbox(lines=4, interactive=False,
                                       label="Voiceover script")
                v2_structure = gr.Markdown("*waiting…*")
            with gr.Accordion("🎤 Voice", open=True):
                v2_voice = gr.Markdown("*waiting…*")
                v2_vo_audio = gr.Audio(label="Voiceover audio (full track)",
                                       interactive=False)
            with gr.Accordion("👤 Character reference", open=True):
                v2_char_ref = gr.Image(label="Identity-locked portrait",
                                       interactive=False, height=400)
            with gr.Accordion("🎞 Anchor frames (per-beat)", open=True):
                v2_anchors = gr.Gallery(
                    label="Beat anchors", columns=4, height=320,
                    allow_preview=True,
                )
            with gr.Accordion("💰 Cost", open=True):
                v2_cost = gr.Markdown("*priced when the run finishes*")
            with gr.Accordion("🧬 Character description (JSON)", open=False):
                v2_character = gr.Code(label="character", language="json")
            with gr.Accordion("🔍 AI review (defect audit)", open=False):
                v2_qc = gr.Markdown("*runs after the final render*")

    # Output tuple order (must match outputs= below):
    #   v2_log, v2_video, v2_script, v2_structure, v2_voice, v2_vo_audio,
    #   v2_char_ref, v2_anchors, v2_character, v2_qc, v2_cost
    _OUTPUT_KEYS = (
        "log", "video", "script", "structure", "voice", "vo_audio",
        "char_ref", "anchors", "character", "qc", "cost",
    )

    def _out(**kw) -> tuple:
        """Build the outputs tuple — unspecified keys are no-op updates."""
        return tuple(
            gr.update(value=kw[k]) if k in kw else gr.update()
            for k in _OUTPUT_KEYS
        )

    async def on_generate_v2(brief_str, hook_sel, narr_sel, video_gen_sel,
                             voice_prov_sel, voice_src_sel, vid,
                             prod_url, prod_file, engine_sel, trim_sil,
                             llm_sel, clip_res_sel, broll_prov_sel, gran_sel,
                             captions_on, grade_on, room_tone_on,
                             transition_sel, punch_in_on,
                             look_sel, color_match_on, qc_on):
        log = ""

        def _ts(msg):
            return f"[{time.strftime('%H:%M:%S')}] {msg}"

        if not (brief_str or "").strip():
            yield _out(log="ERROR: brief is required")
            return
        try:
            settings = load_settings()
        except RuntimeError as e:
            yield _out(log=_ts(f"CONFIG ERROR: {e}"))
            return

        # Resolve product reference (URL wins; else upload the file).
        product_resolved = (prod_url or "").strip() or None
        if not product_resolved and prod_file is not None:
            local = _file_path(prod_file)
            if local:
                try:
                    product_resolved = make_uploader(settings).upload(local)
                except Exception as e:
                    yield _out(log=_ts(f"PRODUCT UPLOAD FAILED: {e}"))
                    return

        # Voice resolution. Raw textbox override always wins; else the Voice
        # source dropdown selects auto-match / a specific clone / catalog.
        voice_override = (vid or "").strip() or None
        use_voice_library: bool | None = None   # None → follow config default
        src = voice_src_sel or "auto"
        if not voice_override:
            if src == "catalog":
                use_voice_library = False
            elif src.startswith("clone:"):
                voice_override = src.split(":", 1)[1]
            else:  # "auto"
                use_voice_library = True
        voice_prov = voice_prov_sel or VOICE_PROVIDER
        yield _out(log=_ts(f"voice provider: {voice_prov}") + "\n")
        if voice_override:
            yield _out(log=_ts(f"voice override: {voice_override}") + "\n")
        elif use_voice_library is False:
            yield _out(log=_ts("voice source: legacy catalog") + "\n")
        else:
            yield _out(log=_ts("voice source: auto-match cloned voice") + "\n")

        ts = time.strftime("%Y%m%d-%H%M%S")
        # ROOT-anchored, NOT cwd-relative: resumable_runs() scans
        # WORKFLOWS_ROOT, so a run written under some other cwd would be
        # invisible to the resume dropdown — i.e. its paid clips unsalvageable.
        work = WORKFLOWS_ROOT / f"v2-{ts}"
        out = work / "final.mp4"

        queue: asyncio.Queue = asyncio.Queue()

        # Mirror callbacks to stderr so the terminal ALSO sees activity
        # (Gradio's streaming generator captures the queue otherwise).
        # This is what makes `caffeinate -i uv run main.py` useful too —
        # progress is visible even when the browser session drops.
        def _log_cb(m):
            print(f"[v2 {time.strftime('%H:%M:%S')}] {m}",
                  file=sys.stderr, flush=True)
            queue.put_nowait(("log", m))

        def _asset_cb(k, p):
            print(f"[v2 {time.strftime('%H:%M:%S')}] asset:{k}",
                  file=sys.stderr, flush=True)
            queue.put_nowait(("asset", k, p))

        async def _run():
            try:
                res = await generate_ugc_video(
                    brief=brief_str, target_duration_s=None,
                    voice_id=voice_override, settings=settings,
                    work_dir=work, out_path=out,
                    product_ref_url=product_resolved,
                    hook_type=None if hook_sel == "auto" else hook_sel,
                    narrative_structure=(
                        None if narr_sel == "auto" else narr_sel
                    ),
                    video_generator=video_gen_sel or VIDEO_GENERATOR,
                    lipsync_engine=engine_sel,
                    lipsync_granularity=gran_sel,
                    use_voice_library=use_voice_library,
                    voice_provider=voice_prov,
                    llm_provider=llm_sel,
                    trim_silence=bool(trim_sil),
                    clip_resolution=clip_res_sel,
                    broll_video_provider=broll_prov_sel,
                    burn_captions=bool(captions_on),
                    finishing_grade=bool(grade_on),
                    finishing_look=look_sel,
                    color_match=bool(color_match_on),
                    visual_qc=bool(qc_on),
                    room_tone=bool(room_tone_on),
                    transition_style=transition_sel,
                    punch_in_accent=bool(punch_in_on),
                    on_progress=_log_cb,
                    on_asset=_asset_cb,
                )
                queue.put_nowait(("done", res))
            except Exception as e:
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                queue.put_nowait(("error", f"{type(e).__name__}: {e}"))

        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                kind = item[0]
                if kind == "log":
                    log += _ts(item[1]) + "\n"
                    yield _out(log=log)
                elif kind == "asset":
                    asset_kind, payload = item[1], item[2]
                    if asset_kind == "timeline":
                        tl = payload   # dict from VideoTimeline.model_dump
                        structure_md = (
                            f"**Hook:** `{tl.get('hook_type')}`  ·  "
                            f"**Narrative:** `{tl.get('narrative_structure')}`  ·  "
                            f"**Target:** {tl.get('target_duration_s')}s  ·  "
                            f"**Beats:** {len(tl.get('beats') or [])}"
                        )
                        yield _out(
                            script=tl.get("full_script", ""),
                            structure=structure_md,
                        )
                    elif asset_kind == "character":
                        yield _out(
                            character=json.dumps(payload, indent=2),
                        )
                    elif asset_kind == "voice":
                        v = payload
                        reason = v.get("match_reason") or ""
                        yield _out(
                            voice=(
                                f"**Voice:** `{v['voice_id']}`  ·  "
                                f"**Gender:** {v['gender']}  ·  "
                                f"**Vibe:** `{v['vibe']}`  ·  "
                                f"**Source:** `{v.get('source', 'catalog')}`"
                                + (f"  \n_match: {reason}_" if reason else "")
                            ),
                        )
                    elif asset_kind == "qc_report":
                        rep = payload
                        lines = [
                            f"**Verdict:** `{rep.get('verdict')}`  ·  "
                            + "  ·  ".join(
                                f"{k}: {v}/2"
                                for k, v in (rep.get("scores") or {}).items()
                            )
                        ]
                        for it in (rep.get("issues") or []):
                            lines.append(
                                f"- **[sev{it.get('severity')}] "
                                f"{it.get('category')}** "
                                f"`{it.get('timestamp')}` — "
                                f"{it.get('issue')}\n"
                                f"  *evidence:* {it.get('evidence')}\n"
                                f"  *fix:* {it.get('suggested_fix')}"
                            )
                        yield _out(qc="\n".join(lines))
                    elif asset_kind == "cost":
                        yield _out(cost=format_cost_markdown(payload))
                    elif asset_kind == "vo_audio":
                        yield _out(vo_audio=payload)   # local mp3 path
                    elif asset_kind == "char_ref":
                        yield _out(char_ref=payload)   # S3 URL
                    elif asset_kind == "anchors":
                        # payload = list of dicts {url, shot_type, framing/broll_type, section}
                        tiles = [
                            (a["url"],
                             f"#{i} · {a['section']} · "
                             + (a.get("framing") or a.get("broll_type") or "—"))
                            for i, a in enumerate(payload)
                        ]
                        yield _out(anchors=tiles)
                    # unknown asset kinds: ignore silently
                elif kind == "done":
                    res = item[1]
                    log += _ts(f"DONE → {res.final_video_path}") + "\n"
                    yield _out(log=log, video=res.final_video_path)
                    return
                elif kind == "error":
                    log += _ts(f"PIPELINE ERROR: {item[1]}") + "\n"
                    yield _out(log=log)
                    return
        finally:
            if not task.done():
                task.cancel()

    gen_btn.click(
        on_generate_v2,
        inputs=[brief, hook_choice, narrative_choice, video_generator,
                voice_provider, voice_source, voice_id,
                product_url, product_file, lipsync_engine, trim_silence,
                script_llm, clip_resolution, broll_video_provider,
                lipsync_granularity,
                burn_captions_cb, finishing_grade_cb, room_tone_cb,
                transition_style_sel, punch_in_cb,
                finishing_look_sel, color_match_cb, visual_qc_cb],
        outputs=[v2_log, v2_video, v2_script, v2_structure, v2_voice,
                 v2_vo_audio, v2_char_ref, v2_anchors, v2_character, v2_qc,
                 v2_cost],
    )


# ===================================================================
# Resume — finish a run whose clips were already paid for
# ===================================================================

def _resume_choices() -> tuple[list[tuple[str, str]], str]:
    """Dropdown choices + a note listing the runs that CANNOT be resumed.

    resumable_runs() touches nothing but the local jobs/ tree (tiers 2 and 3
    are only distinguishable after probing EvoLink, which the preview does), so
    this is cheap enough to re-run on every refresh click — that is what makes
    the dropdown refreshable without restarting the app.

    Blocked runs are NOT silently dropped: they still hold paid clips on disk,
    and the user needs to see why they can't be salvaged.
    """
    try:
        runs = resumable_runs()
    except Exception as e:   # noqa: BLE001 — a broken jobs/ tree, or one
        # manifest.json that is not even JSON, must not take the whole tab down
        # at Blocks-build time (this runs at import of the layout).
        return [], f"*could not scan `jobs/workflows`: {e}*"
    choices = [(r.label, r.run_dir) for r in runs if r.resumable]
    blocked = [r for r in runs if not r.resumable]
    if not choices and not blocked:
        note = "*no partially-paid runs found — nothing to resume.*"
    else:
        note = (f"*{len(choices)} resumable run(s).*"
                if choices else "*no run can be resumed.*")
        if blocked:
            note += (
                "\n\n**Not resumable** — these still hold paid clips on disk, "
                "but their manifest can't be rehydrated:\n"
                + "\n".join(f"- `{r.run_id}` — {r.blocked_reason}"
                            for r in blocked)
            )
    return choices, note


def _build_resume_tab() -> None:
    """Finish a run that died after EvoLink had already been paid.

    Every clip already on disk (or still fetchable from its paid task) is
    reused for free; only the genuine gaps are re-ordered. The preview button
    spends nothing — it polls the task records and range-GETs their artifacts,
    both free — so the cost is on screen BEFORE the user commits.
    """
    gr.Markdown(
        "### Resume a partially-paid run\n"
        "Picks up a Workflow-v2 run that never reached `final.mp4`. Per beat: "
        "**tier 1** a valid local `clip-N.mp4` (free) → **tier 2** re-download "
        "from the paid EvoLink task while the artifact is still live (free, "
        "~72h retention) → **tier 3** re-order (the only step that spends). "
        "Already-paid lipsync passes are reused from the manifest too. "
        "Hit *Preview* first: it quotes the tier-3 spend without spending it."
    )
    _init_choices, _init_note = _resume_choices()
    with gr.Row():
        # ── LEFT: pick the run + assembly options ─────────────────
        with gr.Column(scale=1):
            run_sel = gr.Dropdown(
                choices=_init_choices,
                value=_init_choices[0][1] if _init_choices else None,
                label="Resumable run (newest first)",
            )
            resume_note = gr.Markdown(_init_note)
            with gr.Row():
                refresh_btn = gr.Button("↻ Refresh list", size="sm")
                preview_btn = gr.Button("Preview cost (free)")
            allow_paid_cb = gr.Checkbox(
                value=True,
                label="Allow paid re-orders (off = refuse before any "
                      "create_task, so the run only finishes if it's $0)",
            )
            with gr.Accordion("🎬 Finishing", open=False):
                # Resume re-renders the assembly, so these are live knobs here
                # too — same components/defaults as the Workflow v2 tab.
                with gr.Row():
                    r_captions_cb = gr.Checkbox(
                        value=BURN_CAPTIONS,
                        label="Burn captions (auto-disabled on a manifest that "
                              "predates the persisted VO alignment)",
                    )
                    r_grade_cb = gr.Checkbox(
                        value=FINISHING_GRADE,
                        label="Phone-capture grade (contrast + grain)",
                    )
                    r_room_tone_cb = gr.Checkbox(
                        value=ROOM_TONE,
                        label="Room tone (no dead-silent gaps)",
                    )
                with gr.Row():
                    r_look_sel = gr.Radio(
                        choices=[("Clean bright (safe default)", "clean_bright"),
                                 ("Warm golden-hour", "warm_golden"),
                                 ("Teal-orange (punchy — use sparingly)",
                                  "teal_orange"),
                                 ("No look (grain only)", "none")],
                        value=FINISHING_LOOK,
                        label="Colour look",
                    )
                    r_color_match_cb = gr.Checkbox(
                        value=COLOR_MATCH_CLIPS,
                        label="Shot matching (match clips' exposure/cast "
                              "to the median)",
                    )
                with gr.Row():
                    r_transition_sel = gr.Radio(
                        choices=[("Zoom punch (UGC standard)", "punch"),
                                 ("Whip slide", "slide"),
                                 ("Plain hard cut", "cut")],
                        value=TRANSITION_STYLE,
                        label="Cut accent on b-roll",
                    )
                    r_punch_in_cb = gr.Checkbox(
                        value=PUNCH_IN_ACCENT,
                        label="Product-reveal punch-in (12% tighter framing)",
                    )
                    r_qc_cb = gr.Checkbox(
                        value=VISUAL_QC,
                        label="AI review (Gemini defect audit, report-only)",
                    )
            resume_btn = gr.Button("Resume run", variant="primary")

        # ── RIGHT: plan, streaming progress, final video ──────────
        with gr.Column(scale=2):
            resume_plan_md = gr.Markdown("*preview a run to see its plan*")
            resume_log = gr.Textbox(label="Progress", lines=14,
                                    interactive=False, autoscroll=True)
            resume_video = gr.Video(label="🎬 Final video", interactive=False)

    def on_refresh():
        choices, note = _resume_choices()
        return (
            gr.update(choices=choices,
                      value=choices[0][1] if choices else None),
            note,
        )

    async def on_preview(run_dir):
        if not run_dir:
            return ("*select a run first*",
                    _log("", "ERROR: select a run first"))
        try:
            settings = load_settings()
        except RuntimeError as e:
            return "*config error*", _log("", f"CONFIG ERROR: {e}")
        log = _log("", f"probing {Path(run_dir).name} (free — no orders)")
        lines: list[str] = []
        try:
            plan = await resume_plan(run_dir, settings=settings,
                                     on_progress=lines.append)
        except Exception as e:   # noqa: BLE001 — surfaced in the log, not raised
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            for line in lines:
                log = _log(log, line)
            return ("*plan failed*",
                    _log(log, f"PLAN ERROR: {type(e).__name__}: {e}"))
        for line in lines:
            log = _log(log, line)
        md = (
            f"**{plan['run_id']}** · {plan['n_beats']} beats · "
            f"engine `{plan['lipsync_engine']}`\n\n"
            f"free (tier 1/2): **{len(plan['free'])}** · "
            f"re-order (tier 3): **{len(plan['paid'])}** · "
            f"estimated spend: **${plan['paid_usd']:.2f}**\n\n"
            f"```\n{plan['summary']}\n```\n"
            "*Tier 2 is a prediction: EvoLink keeps the task record long after "
            "the file 404s, so an artifact that is live now can still expire "
            "before you resume — that beat then falls to tier 3 and costs "
            "more than quoted.*"
        )
        if plan["unrecoverable"]:
            md += (f"\n\n⚠️ beats {plan['unrecoverable']} have no anchor image "
                   f"and/or motion prompt in the manifest — this run died "
                   f"before stage 5b and **resume will fail**.")
        return md, _log(log, f"plan ready — ${plan['paid_usd']:.2f} to finish")

    async def on_resume(run_dir, allow_paid, captions_on, grade_on,
                        room_tone_on, look_sel, color_match_on,
                        transition_sel, punch_in_on, qc_on):
        if not run_dir:
            yield _log("", "ERROR: select a run first"), gr.update()
            return
        try:
            settings = load_settings()
        except RuntimeError as e:
            yield _log("", f"CONFIG ERROR: {e}"), gr.update()
            return

        queue: asyncio.Queue = asyncio.Queue()

        # Same streaming shape as on_generate_v2: callbacks are mirrored to
        # stderr so the terminal still shows progress if the browser drops.
        def _log_cb(m):
            print(f"[resume {time.strftime('%H:%M:%S')}] {m}",
                  file=sys.stderr, flush=True)
            queue.put_nowait(("log", m))

        async def _run():
            try:
                res = await resume_ugc_video(
                    run_dir, settings=settings,
                    allow_paid_reorder=bool(allow_paid),
                    burn_captions=bool(captions_on),
                    finishing_grade=bool(grade_on),
                    finishing_look=look_sel,
                    color_match=bool(color_match_on),
                    visual_qc=bool(qc_on),
                    room_tone=bool(room_tone_on),
                    transition_style=transition_sel,
                    punch_in_accent=bool(punch_in_on),
                    on_progress=_log_cb,
                )
                queue.put_nowait(("done", res))
            except Exception as e:
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                queue.put_nowait(("error", f"{type(e).__name__}: {e}"))

        log = _log("", f"resuming {Path(run_dir).name}")
        yield log, gr.update()
        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item[0] == "log":
                    log = _log(log, item[1])
                    yield log, gr.update()
                elif item[0] == "done":
                    res = item[1]
                    log = _log(log, f"DONE → {res.final_video_path}")
                    yield log, gr.update(value=res.final_video_path)
                    return
                elif item[0] == "error":
                    log = _log(log, f"RESUME ERROR: {item[1]}")
                    yield log, gr.update()
                    return
        finally:
            if not task.done():
                task.cancel()

    refresh_btn.click(on_refresh, outputs=[run_sel, resume_note])
    preview_btn.click(on_preview, inputs=[run_sel],
                      outputs=[resume_plan_md, resume_log])
    resume_btn.click(
        on_resume,
        inputs=[run_sel, allow_paid_cb, r_captions_cb, r_grade_cb,
                r_room_tone_cb, r_look_sel, r_color_match_cb,
                r_transition_sel, r_punch_in_cb, r_qc_cb],
        outputs=[resume_log, resume_video],
    )


# ===================================================================
# Top-level — tabbed Blocks
# ===================================================================

def build_app() -> gr.Blocks:
    with gr.Blocks(title="Video-Gen · EvoLink Seedance 2.0 + Nano Banana") as app:
        gr.Markdown("# Video-Gen · EvoLink Seedance 2.0 + Nano Banana")
        with gr.Tabs():
            with gr.Tab("Workflow v2 (Phase 4)"):
                _build_workflow_v2_tab()
            with gr.Tab("Resume run"):
                _build_resume_tab()
            with gr.Tab("Workflow (FLF2V)"):
                _build_workflow_tab()
            with gr.Tab("Direct video"):
                _build_direct_video_tab()
            with gr.Tab("Images"):
                _build_gallery_tab()
    return app
