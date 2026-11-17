# Video-Gen — Gradio app for BytePlus Seedance 2.0

A local Gradio app that submits text + multimodal references (images, videos,
audio) to BytePlus Seedance 2.0, polls the async task to completion, and shows
the generated video in the page.

API reference: see `docs/video-gen.md` and `docs/seedream-tutorial.md`.

## Goals

- Single-page form: prompt + up to 9 images + up to 3 videos + up to 3 audios.
- Each upload has an explicit `role` (first_frame, last_frame,
  reference_image, reference_video, reference_audio).
- Settings: model, aspect ratio, duration, resolution, generate_audio,
  watermark, return_last_frame, seed.
- Submit → block on async task → render result inline.
- Per-job JSON log on disk for inspection / re-runs.

## Non-goals

- Multi-user auth, queueing, hosted deployment.
- Frame-level video editing UI (use prompt + role tags instead).
- Webhook callbacks (we poll).

## Architecture

```
video-gen/
├── main.py                # Entrypoint: launches Gradio app
├── app/
│   ├── __init__.py
│   ├── config.py          # Loads .env; exposes ARK_API_KEY + AWS_*
│   ├── uploader.py        # S3 upload (boto3) → public URL
│   ├── generator.py       # BytePlus client: create_task, poll_task
│   ├── jobs.py            # Writes jobs/{task_id}.json
│   └── ui.py              # Gradio Blocks + event wiring
├── jobs/                  # gitignored — per-task JSON
├── .env                   # gitignored — secrets
├── .env.example           # committed template
├── pyproject.toml         # adds: gradio, boto3, byteplus-python-sdk-v2, python-dotenv
└── docs/                  # this file + API references
```

Each module has one responsibility:

- `config.py` — read env, fail fast if `ARK_API_KEY` is missing.
- `uploader.py` — content-addressable S3 upload, returns a public URL.
- `generator.py` — wraps the BytePlus SDK; knows nothing about S3 or Gradio.
- `jobs.py` — JSON I/O for the run log.
- `ui.py` — Gradio Blocks; orchestrates the other modules.

`ui.py` is the only module that imports from the others.

## Data flow (one generation)

1. **Validate** the form (see "Validation" below). Bail with a UI error if the
   request is malformed; do not call any external service.
2. **Upload assets** via `uploader.upload(local_path)`:
   - Compute sha256 of file content.
   - S3 key: `product_flatlays/crops/{sha256}.{ext}`.
   - Bucket: `upload-data-shoppin-as1`, region `ap-south-1`.
   - If the object already exists (HEAD 200), skip the PUT and reuse the URL.
   - Return `https://{bucket}.s3.{region}.amazonaws.com/{key}`.
3. **Create task** via `generator.create_task(prompt, assets, settings)`:
   - Builds the `content` list: a `text` item, then one item per asset with
     the right `type` (`image_url` / `video_url` / `audio_url`), the URL, and
     the `role`.
   - Calls `client.content_generation.tasks.create(...)` with kwargs:
     `model`, `content`, `generate_audio`, `ratio`, `duration`, `resolution`,
     `watermark`, `return_last_frame`, `seed` (if set).
   - Returns the task_id and the dict-serialized response.
4. **Write initial job JSON** to `jobs/{task_id}.json`:
   ```json
   {
     "task_id": "cgt-...",
     "created_at": "2026-05-20T...",
     "prompt": "...",
     "assets": [{"url": "...", "type": "image_url", "role": "..."}],
     "settings": {"model": "...", "ratio": "...", "duration": 5, ...},
     "status_history": [{"at": "...", "status": "queued"}],
     "result": null
   }
   ```
5. **Poll** via `generator.poll_until_done(task_id, on_status=cb)`:
   - `GET /contents/generations/tasks/{id}` every 10s.
   - Each poll appends to `status_history` in the job JSON.
   - `cb(status)` updates `gr.Progress` in the UI.
   - Terminal states: `succeeded` (returns full response) or `failed`
     (raises with the error payload).
6. **Write final job JSON** with the full response (video_url, seed,
   resolution, ratio, duration, fps, tokens, timestamps).
7. **Render result** in the UI: `gr.Video(video_url)` + download link +
   collapsible JSON view.

## UI

Two-column Gradio Blocks layout.

**Left (inputs)**

- `gr.Textbox(lines=4)` — prompt.
- Image slots × 9 (pre-allocated, hidden until used). Each slot:
  `gr.File` + `gr.Dropdown(["reference_image", "first_frame", "last_frame"])`
  + remove button. `[+ Add image]` reveals the next hidden slot.
- Video slots × 3 — same pattern, role fixed to `reference_video`.
- Audio slots × 3 — same pattern, role fixed to `reference_audio`.
- Settings block:
  - Model: `dreamina-seedance-2-0-260128` (default) or
    `dreamina-seedance-2-0-fast-260128`.
  - Ratio: `21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16 / adaptive` (default `16:9`).
  - Duration: slider 4–15, default 5.
  - Resolution: `480p / 720p / 1080p`. Switching to Fast clamps to
    `480p / 720p`.
  - Checkboxes: `generate_audio` (default true), `watermark` (default false),
    `return_last_frame` (default false).
  - Seed: optional integer; blank = let the API pick.
- `Generate` button.

**Right (output)**

- Status line + progress bar (driven by `gr.Progress`).
- `gr.Video` player (hidden until success).
- Download link / URL display.
- Collapsible `gr.JSON` with the full job record.

**Dynamic rows.** Pre-allocate max slots and toggle `visible` via `gr.State`.
Gradio's reactive model fights dynamic component creation; pre-allocation is
the boring path that actually works.

## Validation

Done client-side before any upload or API call:

- ≤ 9 images, ≤ 3 videos, ≤ 3 audios.
- At most one `first_frame`; at most one `last_frame`.
- If any image has role `first_frame` or `last_frame`, no image may have role
  `reference_image` (different modes, mixing is undefined).
- `duration ∈ [4, 15]`.
- Reject "text + audio only" and "audio only" combinations (per docs).
- If model is `*-fast-*`, reject `resolution == "1080p"`.
- Prompt must be non-empty. (The docs forbid "audio only" and "text + audio
  only"; the simplest enforceable rule that covers both is to always require
  a prompt.)

Validation errors render as red text under the form; the Generate button
remains enabled so the user can fix and retry.

## Error handling

- Missing `ARK_API_KEY` → app refuses to start; clear stderr message.
- Missing AWS credentials → app starts but generation fails at upload time
  with a UI error pointing at `.env`.
- S3 upload failure → job is not created; show the boto3 error message.
- Task creation failure → show the API error verbatim in the UI; nothing is
  written to `jobs/`.
- Poll returns `failed` → write the failure to the job JSON and surface the
  `error.message` in the UI.
- Network blip during polling → retry the single GET up to 3 times with
  exponential backoff before giving up.
- Local network drop / Gradio reload mid-poll → the partial job JSON on disk
  is the recovery breadcrumb; a future "resume by task_id" feature can read
  it.

## Testing

Manual, since this is a personal tool with external API calls:

- Happy path: text-only → video plays inline, job JSON has `succeeded`.
- Image-to-video: one image with role `first_frame` → video uses it.
- Multimodal: 2 images + 1 video + 1 audio with mixed roles → request body
  has the expected shape (verify via the job JSON, which records the assets
  array).
- Validation: try 10 images / mixing first_frame with reference_image / Fast
  + 1080p → all blocked before any network call.
- Upload dedup: submit the same image twice → second submission skips the
  PUT (verify via timing or boto3 debug log).

No automated tests in v1.

## Secrets

`.env` (gitignored):

```
ARK_API_KEY=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_BUCKET_NAME=upload-data-shoppin-as1
AWS_REGION=ap-south-1
```

`.env.example` (committed) mirrors this with empty values.

## Out of scope (future work)

- Multi-user, hosted deployment.
- Job history browser within the UI.
- Resume-by-task-id.
- Webhook receiver (replaces polling).
- Video editing and video extension modes (the data shape supports it, but
  the UI doesn't yet expose those as distinct flows).
