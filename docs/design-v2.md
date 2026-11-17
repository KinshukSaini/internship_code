# Video-Gen v2 — 3-tab pipeline with AI-orchestrated workflow

Extends the existing Seedance-only Gradio app (`docs/design.md`) with:

- **Tab 1 — Full Workflow.** Brief + duration → Gemini drafts script → user
  approves → Seedream generates character ref + first frame → Gemini Pro writes
  Seedance prompts → user approves bundle → Seedance generates N chained 15s
  chunks with `last_frame → first_frame` continuity.
- **Tab 2 — Direct Video Generation.** Today's UI, untouched.
- **Tab 3 — Seedream Image Generation + Gallery.** Prompt → image → saved to
  local gallery (`jobs/images.json`). Workflow-generated images also land here
  with their prompts.

The whole AI orchestration uses Gemini: cheap Flash for mechanical work, Pro
for the one heavy Seedance-prompt-synthesis task that consumes the existing
`video-seedream-skill.md` as cached system context.

## Goals

- One Gradio app, three tabs, shared `app/` library.
- Full workflow runs end-to-end from a one-paragraph brief.
- Continuity across multi-chunk videos: identity (character reference image
  passed on every chunk) + temporal (chunk N's last frame becomes chunk N+1's
  first frame).
- All AI-generated artifacts (scripts, character descriptions, Seedance
  prompts, images, videos) are persisted as JSON so any run can be inspected,
  edited, or re-run.
- Two user approval gates only: script and asset bundle. No micro-management.

## Non-goals

- Final video stitching beyond optional ffmpeg concat — chunks are usable
  individually.
- Hosted multi-user deployment.
- Webhook callbacks; we poll.
- Long-term cloud storage for the image gallery — local only.

## Sub-project decomposition + build order

| # | Sub-project | Files (new unless noted) | Depends on | Parallel with |
|---|---|---|---|---|
| **A** | Seedream gen + gallery | `app/seedream.py`, `app/gallery.py`, `jobs/images.json`, Tab 3 in `app/ui.py` | nothing new | B, C |
| **B** | Gemini integration | `app/gemini.py` (4 async pure functions) | nothing new | A, C |
| **C** | Multi-chunk chaining | `app/chain.py`; small additions to `app/generator.py` | existing Seedance client | A, B |
| **D** | Full Workflow UI + orchestrator | `app/workflow.py`, Tab 1 in `app/ui.py` | A + B + C complete | — (sequenced after) |

Build A, B, C in parallel via three isolated git worktrees (one subagent
each). When all three land, build D on the integrated trunk.

## Data flow — Full Workflow (Tab 1)

```
INPUT:
  - brief: str (e.g. "ad for Goat Life Oats, hook-pivot-CTA")
  - duration_s: int (4..60)
  - product_ref: file upload OR url OR None
        │
        ▼
[1] Gemini Flash · generate_script(brief, duration_s)
    → ScriptDraft { full_script, tone_notes, n_chunks }
        │
        ▼
[2] USER GATE #1 — review/edit script, click Approve
        │
        ▼
[3] Gemini Flash · parallel ×3
    a) split_script(script, n_chunks)
         → list[ChunkSpec { t_start, t_end, dialogue, action, prev_summary }]
    b) describe_character(brief, script)
         → CharacterDescription (structured)
    c) pick_framing(brief, script)
         → one of: selfie / tripod / counter / walking
        │
        ▼
[4] Seedream · parallel ×2
    a) gen_character_ref(char_desc)
         → CharacterRefImage { url, local_path, prompt }
    b) gen_first_frame(char_desc, chunks[0], framing, product_ref)
         → FirstFrameImage { url, local_path, prompt }
    (both auto-added to gallery with source="workflow")
        │
        ▼
[5] Gemini Pro (system_instruction = cached skill MD) · sequential per chunk
    For i in 0..N-1:
        write_seedance_prompt(chunks[i], char_desc, framing,
                              prev_summary=chunks[i-1].summary if i>0 else None)
        → SeedancePrompt (string)
        │
        ▼
[6] USER GATE #2 — review asset bundle:
        - character ref image (replaceable)
        - first frame image  (replaceable)
        - N chunk prompts    (each editable)
    Click Approve
        │
        ▼
[7] Seedance chain · sequential per chunk
    inputs for chunk i:
        reference_image = character_ref.url
        reference_image = product_ref.url (if provided)
        first_frame =
            first_frame.url        if i == 0
            last_frame_(i-1).url   otherwise
        return_last_frame = (i < N-1)
    On chunk i failure:
        - persist partial state to wf-{id}.json
        - PAUSE; surface error in UI; per-chunk Retry button
        │
        ▼
[8] RESULT — N video players (appearing as they complete) + JSON dump.
    A "Stitch to single mp4 (ffmpeg)" button concatenates chunks losslessly
    on demand; the chunks remain individually downloadable.
```

### Continuity guarantees

- **Identity:** `character_ref.url` is attached to every chunk as
  `role=reference_image`. Same URL → same face.
- **Temporal:** `last_frame_(i-1).url` is attached to chunk i as
  `role=first_frame`. Smooth mid-sentence carryover.
- **Action:** `chunks[i-1].summary` is fed to the Gemini Pro call that writes
  chunk i's Seedance prompt, so "she picks up the bowl" in chunk 0 informs
  "she sets the bowl down" in chunk 1.

### Tab 3 unification

Every image produced in step [4] is added to the same `jobs/images.json` that
Tab 3 reads — with `source="workflow"`, the original brief stored alongside,
and a thumbnail. The gallery in Tab 3 shows both ad-hoc generations and
workflow assets.

## Component definitions

### A. Seedream gen + gallery

`app/seedream.py`
```python
@dataclass(frozen=True)
class GeneratedImage:
    id: str           # short hash
    url: str          # BytePlus signed URL (24h)
    local_path: str   # generated-images/{id}.png
    prompt: str
    size: str
    source: str       # "tab3" | "workflow"
    created_at: str   # ISO

class SeedreamClient:
    def __init__(self, settings: AppSettings): ...
    async def generate(self, prompt: str, size: str = "1440x2560",
                       n: int = 1) -> list[GeneratedImage]: ...
```

`app/gallery.py`
```python
class Gallery:
    def __init__(self, root: Path = Path("jobs"),
                 image_dir: Path = Path("generated-images")): ...
    def add(self, img: GeneratedImage) -> None: ...
    def list(self, source: str | None = None,
             limit: int = 100) -> list[GeneratedImage]: ...
    def get(self, image_id: str) -> GeneratedImage | None: ...
```

Storage: `jobs/images.json` is a JSON array of `GeneratedImage` dicts. Append
on add, read on list. Concurrency: single Gradio process, no contention.

### B. Gemini integration

`app/gemini.py`
```python
GEMINI_FAST_MODEL = "gemini-3.5-flash"
GEMINI_PRO_MODEL = "gemini-3.1-pro-preview"

class ScriptDraft(BaseModel):
    full_script: str
    tone_notes: str
    n_chunks: int

class ChunkSpec(BaseModel):
    index: int
    t_start: float
    t_end: float
    dialogue: str
    action: str
    summary: str          # 1-line summary for continuity

class CharacterDescription(BaseModel):
    age: int
    gender: str
    ethnicity: str
    hair: str
    face: str
    wardrobe: str
    energy: str           # "warm friendly UGC creator"

FramingChoice = Literal["selfie", "tripod", "counter", "walking"]

class GeminiClient:
    def __init__(self, settings: AppSettings): ...

    async def generate_script(self, brief: str,
                              duration_s: int) -> ScriptDraft: ...
    async def split_script(self, script: ScriptDraft,
                           n_chunks: int) -> list[ChunkSpec]: ...
    async def describe_character(self, brief: str,
                                 script: ScriptDraft) -> CharacterDescription: ...
    async def pick_framing(self, brief: str,
                           script: ScriptDraft) -> FramingChoice: ...

    async def write_seedance_prompt(self, chunk: ChunkSpec,
                                    character: CharacterDescription,
                                    framing: FramingChoice,
                                    prev_summary: str | None,
                                    skill_md_cache_name: str) -> str: ...
```

Implementation notes:
- All async; uses `client.aio.models.generate_content`.
- Flash calls use `response_schema=PydanticClass` for guaranteed JSON.
- Pro call uses `system_instruction` referencing a cached blob of
  `video-seedream-skill.md`. Cache created lazily on first Pro call,
  reused on subsequent calls.
- Each call independently testable by mocking `client.aio.models...`.

### C. Multi-chunk chaining

`app/chain.py`
```python
@dataclass
class ChunkPlan:
    seedance_prompt: str
    settings: FormSettings
    first_frame_url: str | None     # None means use first_frame_image
    is_last: bool                   # controls return_last_frame

@dataclass
class ChunkResult:
    index: int
    video_url: str
    last_frame_url: str | None

class ChainFailure(Exception):
    def __init__(self, chunk_index: int,
                 partial_results: list[ChunkResult],
                 underlying: Exception): ...

async def run_chain(
    client: SeedanceClient,
    chunks: list[ChunkPlan],
    character_ref_url: str,
    product_ref_url: str | None,
    on_progress: Callable[[int, str], None],
) -> list[ChunkResult]: ...
```

`generator.py` modification: ensure `return_last_frame=true` causes the polled
response to expose a `last_frame_url` field. Add to `_serialize` if needed.

### D. Full Workflow UI + orchestrator

`app/workflow.py`
```python
WorkflowState = Literal[
    "draft", "script_review", "generating_assets",
    "asset_review", "generating_videos", "complete", "failed"
]

@dataclass
class WorkflowSession:
    id: str
    state: WorkflowState
    brief: str
    duration_s: int
    product_ref: ImageRef | None
    script: ScriptDraft | None
    chunks: list[ChunkSpec] | None
    character: CharacterDescription | None
    framing: FramingChoice | None
    character_ref: GeneratedImage | None
    first_frame: GeneratedImage | None
    seedance_prompts: list[str] | None
    chunk_results: list[ChunkResult]      # appended as chunks succeed
    error: str | None
```

Persistence: `jobs/workflows/wf-{id}.json` written after every state
transition. App startup scans this directory and exposes resumable sessions
(future work; v1 just persists for debugging).

Tab 1 UI: conditional rendering driven by `session.state`. Each state has its
own panel; Approve buttons advance state and trigger the next stage.

## Tab layout

```
┌─ Video-Gen · Seedance 2.0 + Seedream 5.0 ───────────────┐
│ [ Workflow ] [ Direct video ] [ Images ]                │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  (active tab content)                                   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

Tab 2 is the existing `build_app()` body, just nested inside `gr.Tab`.

## File structure (after v2)

```
video-gen/
├── main.py
├── app/
│   ├── __init__.py
│   ├── config.py            # adds GEMINI_API_KEY, model id constants
│   ├── validation.py
│   ├── uploader.py
│   ├── generator.py         # +last_frame_url exposed via _serialize
│   ├── jobs.py
│   ├── seedream.py          # NEW (A)
│   ├── gallery.py           # NEW (A)
│   ├── gemini.py            # NEW (B)
│   ├── chain.py             # NEW (C)
│   ├── workflow.py          # NEW (D)
│   └── ui.py                # +tabs, +Tab 1 wizard, +Tab 3 gallery
├── tests/
│   ├── test_seedream.py
│   ├── test_gallery.py
│   ├── test_gemini.py
│   ├── test_chain.py
│   ├── test_workflow.py
│   └── (existing tests untouched)
├── jobs/
│   ├── images.json          # gallery index
│   ├── workflows/wf-*.json  # per-workflow state
│   └── cgt-*.json           # existing Seedance task logs
├── generated-images/        # local PNG thumbnails + originals
├── video-seedream-skill.md  # consumed by gemini.py as system_instruction
└── docs/design-v2.md        # this file
```

## Model selection

- **`gemini-3.5-flash`** — `generate_script`, `split_script`,
  `describe_character`, `pick_framing`.
- **`gemini-3.1-pro-preview`** — `write_seedance_prompt` only. Heavy reasoning
  + cached skill MD as system instruction.

Both wrapped via `google-genai` SDK (`from google import genai`). API key from
`GEMINI_API_KEY` env var.

Model IDs centralised as constants in `app/config.py` so swapping a dated
suffix is a one-line change.

If either ID is rejected by the API on the first smoke test, fall back to the
most recent stable variant (`gemini-2.5-pro` / `gemini-2.5-flash`) and report
the rejection in the build log.

## Approval gates

Two, exactly:

1. **After [1]** — user edits/approves the draft script.
2. **After [5]** — user reviews the bundled character ref + first frame + N
   Seedance prompts; can edit any prompt before approving.

Everything between these gates runs unattended. Once gate #2 is cleared, the
Seedance chain runs straight through and the videos appear in the result
panel one by one.

## Failure handling

- **Gemini call fails:** the workflow stops at that step, surfaces the
  exception, and offers a "Retry" button. State persisted.
- **Seedream call fails:** same.
- **Seedance chunk fails mid-chain:** the previous chunks' videos and last
  frames are preserved in `wf-{id}.json`. The failed chunk's row in the
  result panel shows the error and a Retry button. User can also edit the
  Seedance prompt for that chunk before retrying.
- **Moderation rejection (`InputImageSensitiveContentDetected`):** treated as
  a normal failure; user can re-roll the character reference image and retry
  the chunk.

## Testing strategy

Per sub-project, TDD-driven unit tests for pure logic; smoke tests for
network calls.

- **A:** unit-test `gallery.add/list/get` with `tmp_path`. Smoke-test
  `seedream.generate` with a real prompt once.
- **B:** unit-test each Gemini function by mocking
  `client.aio.models.generate_content` and asserting schema + system
  instruction shape. Smoke-test one Pro call with real skill MD.
- **C:** unit-test `run_chain` by mocking the Seedance client; verify
  reference URLs are correctly threaded chunk-to-chunk and `return_last_frame`
  is set on all-but-last chunks.
- **D:** state-machine transitions unit-tested; UI is manually smoke-tested
  end-to-end with a 15s and a 30s brief.

## Security

- `GEMINI_API_KEY` joins `ARK_API_KEY` + AWS keys in `.env` (gitignored).
- No user-uploaded files are sent to Gemini.
- Image gallery is local-only; never uploaded to cloud unless used inside a
  video where the existing uploader takes over (or the BytePlus URL is reused
  directly).

## Out of scope (v2)

- Resumable workflows across app restarts (we persist state, but no UI to
  reopen). Add later.
- Multi-user / hosted deployment.
- Real-time webhooks instead of polling.
- Bulk-regenerate gallery cleanup.
