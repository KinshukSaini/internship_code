# Video-Gen v2 — 3-tab pipeline · Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build a 3-tab Gradio app — Full Workflow (Tab 1), existing Direct Video (Tab 2), Seedream Image Gallery (Tab 3) — with Gemini orchestrating multi-chunk video generation with character + temporal continuity. Spec: `docs/design-v2.md`.

**Architecture:** Four new modules under `app/` — `seedream` (image gen), `gallery` (local JSON-backed image store), `gemini` (four async functions for script/character/prompt work), `chain` (sequential Seedance with last_frame→first_frame chaining), `workflow` (state machine for Tab 1). Plus structural changes to `ui.py` (tabs) and small additions to `config.py` and `generator.py`.

**Tech Stack:** Python ≥3.11, `uv`, `gradio` (existing), `google-genai` (new), `pydantic` (transitively via gradio; pinned explicitly), `boto3` (existing), `byteplus-python-sdk-v2` (existing), `python-dotenv` (existing), `pytest` + `pytest-mock` + `pytest-asyncio` (new dev dep).

**Hard user constraints (apply to every task below):**
- **NO git commits.** Skip every commit step. Stop at "tests passing" for each task.
- **NO git worktrees.** All work happens in `/Users/harshkedia/Custom/Personal/video-gen`.
- **Sub-projects A, B, C are independent.** Order them however is convenient. D requires A+B+C done.

---

## File map (everything created/modified by this plan)

```
video-gen/
├── pyproject.toml            # +google-genai, pytest-asyncio
├── .env.example              # +GEMINI_API_KEY
├── app/
│   ├── config.py             # +GEMINI_API_KEY field + model id constants
│   ├── seedream.py           # NEW (A)  — SeedreamClient
│   ├── gallery.py            # NEW (A)  — Gallery + GeneratedImage
│   ├── gemini.py             # NEW (B)  — GeminiClient + 4 async fns
│   ├── chain.py              # NEW (C)  — run_chain
│   ├── generator.py          # SMALL MOD — expose last_frame_url
│   ├── workflow.py           # NEW (D)  — WorkflowSession + orchestrator
│   └── ui.py                 # REWORK   — tabs + Tab 1 wizard + Tab 3 gallery
├── tests/
│   ├── test_seedream.py      # NEW (A)
│   ├── test_gallery.py       # NEW (A)
│   ├── test_gemini.py        # NEW (B)
│   ├── test_chain.py         # NEW (C)
│   └── test_workflow.py      # NEW (D)
├── jobs/
│   ├── images.json           # runtime — gallery index
│   └── workflows/wf-*.json   # runtime — per-workflow state
└── docs/design-v2.md         # already exists
```

---

# PROJECT SETUP (do first, blocks everything)

## Task 0: Add Gemini SDK, async test dep, GEMINI_API_KEY env var, model-id constants

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `app/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 0.1: Add deps to `pyproject.toml`**

Replace the `[project]` and `[dependency-groups]` blocks with:

```toml
[project]
name = "video-gen"
version = "0.1.0"
description = "Gradio frontend for BytePlus Seedance 2.0 video generation"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "gradio>=4.44",
    "boto3>=1.34",
    "byteplus-python-sdk-v2>=1.0.0",
    "python-dotenv>=1.0",
    "sniffio>=1.3.1",
    "google-genai>=1.0",
    "pydantic>=2.6",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
    "pytest-asyncio>=0.23",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
asyncio_mode = "auto"
```

- [ ] **Step 0.2: Sync deps**

Run: `uv sync`
Expected: installs `google-genai`, `pydantic`, `pytest-asyncio`, no errors.

- [ ] **Step 0.3: Add `GEMINI_API_KEY` to `.env.example`**

Read the file, then add this line after `ARK_API_KEY=`:

```
GEMINI_API_KEY=
```

- [ ] **Step 0.4: Write failing tests** in `tests/test_config.py` — APPEND these tests at the bottom (do not delete the existing ones):

```python
def test_load_settings_reads_gemini_key(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-abc")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-xyz")
    s = load_settings()
    assert s.gemini_api_key == "gem-xyz"


def test_load_settings_gemini_optional(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-abc")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = load_settings()
    assert s.gemini_api_key == ""


def test_model_id_constants_exposed():
    from app.config import GEMINI_FAST_MODEL, GEMINI_PRO_MODEL, GEMINI_FAST_FALLBACK, GEMINI_PRO_FALLBACK
    assert GEMINI_FAST_MODEL == "gemini-3.5-flash"
    assert GEMINI_PRO_MODEL == "gemini-3.1-pro-preview"
    assert GEMINI_FAST_FALLBACK == "gemini-2.5-flash"
    assert GEMINI_PRO_FALLBACK == "gemini-2.5-pro"
```

- [ ] **Step 0.5: Run tests, verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 new tests FAIL (existing 3 still pass).

- [ ] **Step 0.6: Implement** — replace `app/config.py` with:

```python
"""Environment-driven settings. Loaded once at startup."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Centralised model IDs so swapping a dated suffix is a one-line change.
GEMINI_FAST_MODEL = "gemini-3.5-flash"
GEMINI_PRO_MODEL = "gemini-3.1-pro-preview"
GEMINI_FAST_FALLBACK = "gemini-2.5-flash"
GEMINI_PRO_FALLBACK = "gemini-2.5-pro"


@dataclass(frozen=True)
class Settings:
    ark_api_key: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_bucket_name: str
    aws_region: str
    gemini_api_key: str


def load_settings() -> Settings:
    load_dotenv(override=False)

    ark_api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not ark_api_key:
        raise RuntimeError(
            "ARK_API_KEY is not set. Add it to .env (see .env.example)."
        )

    return Settings(
        ark_api_key=ark_api_key,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "").strip(),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip(),
        aws_bucket_name=os.environ.get("AWS_BUCKET_NAME", "upload-data-shoppin-as1").strip(),
        aws_region=os.environ.get("AWS_REGION", "ap-south-1").strip(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
    )
```

- [ ] **Step 0.7: Run tests, verify all pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 6 passed (3 existing + 3 new).

(No commit — user constraint.)

---

# SUB-PROJECT A — Seedream image gen + gallery

## Task A1: `app/seedream.py` — SeedreamClient

**Files:**
- Create: `app/seedream.py`
- Create: `tests/test_seedream.py`

- [ ] **Step A1.1: Write failing tests** in `tests/test_seedream.py`

```python
from unittest.mock import patch, MagicMock
import pytest
from app.seedream import SeedreamClient, GeneratedImage
from app.config import Settings


def _settings(**overrides):
    base = dict(
        ark_api_key="ark-x",
        aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r",
        gemini_api_key="",
    )
    base.update(overrides)
    return Settings(**base)


def test_seedream_call_payload_shape():
    """The HTTP POST body must use Seedream's parameter names."""
    captured = {}

    def fake_post(url, body, headers, timeout):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return {
            "model": "seedream-5-0-260128",
            "data": [{"url": "https://x.test/a.jpeg", "size": "1440x2560"}],
        }

    c = SeedreamClient(_settings(ark_api_key="ark-test"), _post=fake_post)
    result = c.generate_sync(prompt="a daisy", size="1440x2560", n=1)

    assert captured["url"].endswith("/api/v3/images/generations")
    assert captured["body"]["model"] == "seedream-5-0-260128"
    assert captured["body"]["prompt"] == "a daisy"
    assert captured["body"]["size"] == "1440x2560"
    assert captured["body"]["n"] == 1
    assert captured["body"]["watermark"] is False
    assert captured["body"]["response_format"] == "url"
    assert captured["headers"]["Authorization"] == "Bearer ark-test"
    assert isinstance(result, list) and len(result) == 1
    assert result[0].url == "https://x.test/a.jpeg"
    assert result[0].prompt == "a daisy"
    assert result[0].size == "1440x2560"


def test_seedream_handles_n_greater_than_one():
    def fake_post(url, body, headers, timeout):
        return {"data": [
            {"url": "https://x/a.jpeg", "size": "1024x1024"},
            {"url": "https://x/b.jpeg", "size": "1024x1024"},
        ]}

    c = SeedreamClient(_settings(), _post=fake_post)
    out = c.generate_sync(prompt="p", size="1024x1024", n=2)
    assert len(out) == 2
    assert out[1].url == "https://x/b.jpeg"


def test_seedream_raises_on_http_error():
    def fake_post(url, body, headers, timeout):
        raise RuntimeError("HTTP 400: bad")
    c = SeedreamClient(_settings(), _post=fake_post)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        c.generate_sync(prompt="p", size="1440x2560", n=1)


def test_generated_image_id_is_deterministic_from_url():
    img = GeneratedImage.from_response(
        url="https://x.test/abc.jpeg",
        prompt="p", size="s", source="tab3",
    )
    img2 = GeneratedImage.from_response(
        url="https://x.test/abc.jpeg",
        prompt="DIFFERENT", size="other", source="workflow",
    )
    # Same URL → same id (we identify by the source URL)
    assert img.id == img2.id
    assert len(img.id) == 16
```

- [ ] **Step A1.2: Run tests, expect fail**

Run: `uv run pytest tests/test_seedream.py -v`
Expected: ImportError.

- [ ] **Step A1.3: Implement** `app/seedream.py`:

```python
"""BytePlus Seedream 5.0 text-to-image client.

Returns GeneratedImage records; persistence is the gallery's job.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.config import Settings

API_URL = "https://ark.ap-southeast.bytepluses.com/api/v3/images/generations"
MODEL_ID = "seedream-5-0-260128"

# Seedream requires images of at least 3,686,400 pixels.
# 1440x2560 = 3,686,400 (vertical 9:16 at 2K minimum).
DEFAULT_SIZE = "1440x2560"


@dataclass(frozen=True)
class GeneratedImage:
    id: str
    url: str
    prompt: str
    size: str
    source: str            # "tab3" | "workflow"
    created_at: str        # ISO8601

    @classmethod
    def from_response(cls, *, url: str, prompt: str, size: str, source: str) -> "GeneratedImage":
        img_id = hashlib.sha256(url.encode()).hexdigest()[:16]
        return cls(
            id=img_id,
            url=url,
            prompt=prompt,
            size=size,
            source=source,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GeneratedImage":
        return cls(**d)


def _default_post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from None


class SeedreamClient:
    def __init__(self, settings: Settings, *,
                 _post: Callable[[str, dict, dict, float], dict] = _default_post):
        if not settings.ark_api_key:
            raise RuntimeError("ARK_API_KEY is required for Seedream.")
        self._api_key = settings.ark_api_key
        self._post = _post

    def generate_sync(
        self, *, prompt: str, size: str = DEFAULT_SIZE,
        n: int = 1, source: str = "tab3",
    ) -> list[GeneratedImage]:
        body = {
            "model": MODEL_ID,
            "prompt": prompt,
            "size": size,
            "n": n,
            "response_format": "url",
            "watermark": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        resp = self._post(API_URL, body, headers, 120.0)
        return [
            GeneratedImage.from_response(
                url=item["url"], prompt=prompt,
                size=item.get("size", size), source=source,
            )
            for item in resp.get("data", [])
        ]
```

- [ ] **Step A1.4: Run tests, expect pass**

Run: `uv run pytest tests/test_seedream.py -v`
Expected: 4 passed.

(No commit.)

## Task A2: `app/gallery.py` — JSON-backed image store

**Files:**
- Create: `app/gallery.py`
- Create: `tests/test_gallery.py`

- [ ] **Step A2.1: Write failing tests** in `tests/test_gallery.py`

```python
import json
from pathlib import Path
from app.gallery import Gallery
from app.seedream import GeneratedImage


def _img(url="https://x/a.jpeg", source="tab3"):
    return GeneratedImage.from_response(
        url=url, prompt="p", size="1440x2560", source=source,
    )


def test_add_persists_image(tmp_path):
    g = Gallery(index_path=tmp_path / "images.json")
    g.add(_img())
    data = json.loads((tmp_path / "images.json").read_text())
    assert len(data) == 1
    assert data[0]["url"] == "https://x/a.jpeg"


def test_list_returns_most_recent_first(tmp_path):
    g = Gallery(index_path=tmp_path / "images.json")
    a = _img(url="https://x/a.jpeg")
    b = _img(url="https://x/b.jpeg")
    g.add(a); g.add(b)
    out = g.list()
    assert [x.url for x in out] == ["https://x/b.jpeg", "https://x/a.jpeg"]


def test_list_filter_by_source(tmp_path):
    g = Gallery(index_path=tmp_path / "images.json")
    g.add(_img(url="https://x/a.jpeg", source="tab3"))
    g.add(_img(url="https://x/b.jpeg", source="workflow"))
    only_workflow = g.list(source="workflow")
    assert len(only_workflow) == 1
    assert only_workflow[0].url == "https://x/b.jpeg"


def test_get_by_id(tmp_path):
    g = Gallery(index_path=tmp_path / "images.json")
    img = _img(url="https://x/a.jpeg")
    g.add(img)
    fetched = g.get(img.id)
    assert fetched is not None and fetched.url == "https://x/a.jpeg"
    assert g.get("not-an-id") is None


def test_add_is_idempotent_by_id(tmp_path):
    """Adding the same image twice does not duplicate."""
    g = Gallery(index_path=tmp_path / "images.json")
    img = _img(url="https://x/a.jpeg")
    g.add(img); g.add(img)
    assert len(g.list()) == 1


def test_missing_file_yields_empty_list(tmp_path):
    g = Gallery(index_path=tmp_path / "does-not-exist.json")
    assert g.list() == []
```

- [ ] **Step A2.2: Run tests, expect fail**

Run: `uv run pytest tests/test_gallery.py -v`
Expected: ImportError.

- [ ] **Step A2.3: Implement** `app/gallery.py`:

```python
"""Local JSON-backed image gallery. Append-only with idempotent add."""
from __future__ import annotations

import json
from pathlib import Path

from app.seedream import GeneratedImage


class Gallery:
    def __init__(self, index_path: Path | str = "jobs/images.json"):
        self._path = Path(index_path)

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return []

    def _write(self, data: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))

    def add(self, img: GeneratedImage) -> None:
        data = self._read()
        if any(d.get("id") == img.id for d in data):
            return
        data.append(img.to_dict())
        self._write(data)

    def list(self, *, source: str | None = None,
             limit: int = 500) -> list[GeneratedImage]:
        rows = list(reversed(self._read()))
        if source is not None:
            rows = [d for d in rows if d.get("source") == source]
        return [GeneratedImage.from_dict(d) for d in rows[:limit]]

    def get(self, image_id: str) -> GeneratedImage | None:
        for d in self._read():
            if d.get("id") == image_id:
                return GeneratedImage.from_dict(d)
        return None
```

- [ ] **Step A2.4: Run tests, expect pass**

Run: `uv run pytest tests/test_gallery.py -v`
Expected: 6 passed.

(No commit.)

---

# SUB-PROJECT B — Gemini integration

## Task B1: `app/gemini.py` — Pydantic schemas + GeminiClient skeleton + `generate_script`

**Files:**
- Create: `app/gemini.py`
- Create: `tests/test_gemini.py`

- [ ] **Step B1.1: Write failing tests** in `tests/test_gemini.py`

```python
from unittest.mock import AsyncMock, MagicMock
import pytest
from app.gemini import (
    GeminiClient, ScriptDraft, ChunkSpec, CharacterDescription,
)
from app.config import Settings, GEMINI_FAST_MODEL, GEMINI_PRO_MODEL


def _settings():
    return Settings(
        ark_api_key="ark", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="gem-test",
    )


def _mock_genai_client(text_value: str):
    """Return a MagicMock that mimics google-genai's client.aio.models.generate_content."""
    fake_resp = MagicMock(text=text_value)
    fake = MagicMock()
    fake.aio.models.generate_content = AsyncMock(return_value=fake_resp)
    return fake


def test_client_requires_api_key():
    s = Settings(ark_api_key="ark", aws_access_key_id="", aws_secret_access_key="",
                 aws_bucket_name="b", aws_region="r", gemini_api_key="")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiClient(s)


@pytest.mark.asyncio
async def test_generate_script_returns_parsed_draft():
    payload = '{"full_script": "hi", "tone_notes": "casual", "n_chunks": 1}'
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)

    draft = await c.generate_script(brief="ad for oats", duration_s=12)

    assert isinstance(draft, ScriptDraft)
    assert draft.full_script == "hi"
    assert draft.n_chunks == 1
    call_kwargs = fake.aio.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == GEMINI_FAST_MODEL
    assert "ad for oats" in call_kwargs["contents"]
    assert "12" in call_kwargs["contents"]
    # response_schema must be set so we get guaranteed JSON
    assert call_kwargs["config"].response_schema is ScriptDraft


@pytest.mark.asyncio
async def test_generate_script_computes_n_chunks_for_long_durations():
    payload = '{"full_script": "x", "tone_notes": "t", "n_chunks": 3}'
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    draft = await c.generate_script(brief="b", duration_s=42)
    # The prompt instructs Gemini that n_chunks = ceil(duration_s / 15)
    assert "ceil(42 / 15)" in fake.aio.models.generate_content.call_args.kwargs["contents"]
```

- [ ] **Step B1.2: Run tests, expect fail**

Run: `uv run pytest tests/test_gemini.py -v`
Expected: ImportError.

- [ ] **Step B1.3: Implement minimal** `app/gemini.py`:

```python
"""Google Gemini integration. Async-only.

Pure functions wrapping client.aio.models.generate_content. Each function
takes plain inputs and returns a structured Pydantic model.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import (
    GEMINI_FAST_MODEL,
    GEMINI_PRO_MODEL,
    Settings,
)


# ---------- Pydantic schemas ----------

class ScriptDraft(BaseModel):
    full_script: str = Field(description="Final dialogue/voiceover, ready to read aloud.")
    tone_notes: str = Field(description="One-sentence direction on tone, pace, energy.")
    n_chunks: int = Field(description="Number of 15-second video chunks needed.")


class ChunkSpec(BaseModel):
    index: int
    t_start: float
    t_end: float
    dialogue: str
    action: str = Field(description="Visual action happening in this chunk.")
    summary: str = Field(description="One-line summary used to inform the next chunk.")


class CharacterDescription(BaseModel):
    age: int
    gender: str
    ethnicity: str
    hair: str
    face: str = Field(description="Skin / face appearance, age-appropriate, photogenic.")
    wardrobe: str
    energy: str


FramingChoice = Literal["selfie", "tripod", "counter", "walking"]


# ---------- Client ----------

class GeminiClient:
    def __init__(self, settings: Settings, *, _client: Any = None):
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if _client is not None:
            self._client = _client
        else:
            # Lazy import so test path doesn't require the SDK installed.
            from google import genai
            self._client = genai.Client(api_key=settings.gemini_api_key)

    # Lazy import inside helpers so tests with a mocked _client never touch types.
    def _config(self, *, schema=None, system_instruction: str | None = None,
                temperature: float = 0.4, max_output_tokens: int = 4096):
        from google.genai import types
        return types.GenerateContentConfig(
            response_mime_type="application/json" if schema is not None else "text/plain",
            response_schema=schema,
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

    async def generate_script(self, *, brief: str, duration_s: int) -> ScriptDraft:
        n_chunks = max(1, math.ceil(duration_s / 15))
        prompt = (
            f"You are a copywriter for short-form UGC video ads. The brief is below.\n"
            f"Target total duration: {duration_s} seconds.\n"
            f"This will be split into N chunks of 15 seconds each (ceil({duration_s} / 15) = {n_chunks}).\n"
            f"Write a single tight script that, when read at fast-paced conversational UGC pace "
            f"(~3.3 words/sec), fits within {duration_s} seconds total. Include hook, pivot, "
            f"product benefits, and CTA. Return JSON matching the schema. Set n_chunks = {n_chunks}.\n\n"
            f"BRIEF:\n{brief}"
        )
        resp = await self._client.aio.models.generate_content(
            model=GEMINI_FAST_MODEL,
            contents=prompt,
            config=self._config(schema=ScriptDraft),
        )
        return ScriptDraft.model_validate_json(resp.text)
```

- [ ] **Step B1.4: Run tests, expect pass**

Run: `uv run pytest tests/test_gemini.py -v`
Expected: 4 passed.

(No commit.)

## Task B2: `gemini.split_script` + `gemini.describe_character` + `gemini.pick_framing`

**Files:**
- Modify: `app/gemini.py`
- Modify: `tests/test_gemini.py`

- [ ] **Step B2.1: Write failing tests** — APPEND to `tests/test_gemini.py`:

```python
@pytest.mark.asyncio
async def test_split_script_returns_chunks():
    payload = (
        '[{"index":0,"t_start":0,"t_end":15,"dialogue":"hi","action":"smile","summary":"intro"},'
        ' {"index":1,"t_start":15,"t_end":30,"dialogue":"bye","action":"wave","summary":"outro"}]'
    )
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    draft = ScriptDraft(full_script="hi. bye.", tone_notes="t", n_chunks=2)
    chunks = await c.split_script(script=draft, n_chunks=2)
    assert len(chunks) == 2
    assert chunks[0].dialogue == "hi"
    assert chunks[1].summary == "outro"
    kw = fake.aio.models.generate_content.call_args.kwargs
    assert kw["model"] == GEMINI_FAST_MODEL


@pytest.mark.asyncio
async def test_describe_character_returns_struct():
    payload = ('{"age":24,"gender":"female","ethnicity":"Indian",'
               '"hair":"curly","face":"smooth","wardrobe":"tee","energy":"warm"}')
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    draft = ScriptDraft(full_script="hi", tone_notes="t", n_chunks=1)
    char = await c.describe_character(brief="oats ad", script=draft)
    assert char.age == 24 and char.ethnicity == "Indian"


@pytest.mark.asyncio
async def test_pick_framing_returns_one_of_four():
    fake = _mock_genai_client('"selfie"')
    c = GeminiClient(_settings(), _client=fake)
    draft = ScriptDraft(full_script="hi", tone_notes="t", n_chunks=1)
    framing = await c.pick_framing(brief="ad", script=draft)
    assert framing == "selfie"


@pytest.mark.asyncio
async def test_pick_framing_invalid_response_falls_back_to_selfie():
    fake = _mock_genai_client('"someBogusValue"')
    c = GeminiClient(_settings(), _client=fake)
    draft = ScriptDraft(full_script="hi", tone_notes="t", n_chunks=1)
    framing = await c.pick_framing(brief="ad", script=draft)
    assert framing == "selfie"
```

- [ ] **Step B2.2: Run tests, expect fail**

Run: `uv run pytest tests/test_gemini.py -v -k "split or describe or framing"`
Expected: 4 FAIL (AttributeError: split_script etc.).

- [ ] **Step B2.3: Implement** — APPEND to `app/gemini.py` (inside `class GeminiClient`):

```python
    async def split_script(self, *, script: ScriptDraft,
                           n_chunks: int) -> list[ChunkSpec]:
        prompt = (
            f"Split the following UGC ad script into exactly {n_chunks} 15-second "
            f"chunks for sequential video generation. For each chunk, fill: "
            f"index (0..{n_chunks - 1}), t_start, t_end, dialogue (verbatim words "
            f"spoken in this chunk), action (visual beat), and summary (one short "
            f"line that the next chunk's prompt can read to maintain continuity).\n\n"
            f"Each chunk must hold ~50 words MAX (fast UGC pace, ~3.3 words/sec × 15s).\n"
            f"Distribute the dialogue so the chunks flow naturally; do not cut "
            f"mid-sentence unless unavoidable.\n\n"
            f"Tone: {script.tone_notes}\n\n"
            f"SCRIPT:\n{script.full_script}\n\n"
            f"Return a JSON array of ChunkSpec objects."
        )
        resp = await self._client.aio.models.generate_content(
            model=GEMINI_FAST_MODEL,
            contents=prompt,
            config=self._config(schema=list[ChunkSpec]),
        )
        import json as _json
        raw = _json.loads(resp.text)
        return [ChunkSpec.model_validate(x) for x in raw]

    async def describe_character(self, *, brief: str,
                                 script: ScriptDraft) -> CharacterDescription:
        prompt = (
            "Given the ad brief and script below, write a hyperrealistic UGC "
            "creator character description: a real Instagram-creator type, "
            "naturally photogenic, age-appropriate to the script. Use AFFIRMATIVE "
            "positive descriptors (smooth healthy young skin, subtle natural glow, "
            "no visible blemishes). Do NOT use words like 'pores, peach fuzz, "
            "no beauty filter' that bias image models toward aged/textured output. "
            "Return JSON matching CharacterDescription.\n\n"
            f"BRIEF:\n{brief}\n\nSCRIPT:\n{script.full_script}"
        )
        resp = await self._client.aio.models.generate_content(
            model=GEMINI_FAST_MODEL,
            contents=prompt,
            config=self._config(schema=CharacterDescription),
        )
        return CharacterDescription.model_validate_json(resp.text)

    async def pick_framing(self, *, brief: str,
                           script: ScriptDraft) -> FramingChoice:
        prompt = (
            "Pick the single best UGC camera framing for this ad. Return the "
            "string only (one of: selfie, tripod, counter, walking).\n"
            "  - selfie: handheld arm-extended phone selfie, intimate confessional energy\n"
            "  - tripod: static medium shot, creator stands hands-free, can hold product\n"
            "  - counter: behind-the-counter static shot, looking down at surface, prep vibe\n"
            "  - walking: walking-and-talking handheld through a space, lifestyle-vlog\n\n"
            f"BRIEF:\n{brief}\n\nSCRIPT:\n{script.full_script}"
        )
        resp = await self._client.aio.models.generate_content(
            model=GEMINI_FAST_MODEL,
            contents=prompt,
            config=self._config(schema=None, temperature=0.2),
        )
        choice = resp.text.strip().strip('"').lower()
        if choice not in ("selfie", "tripod", "counter", "walking"):
            return "selfie"
        return choice  # type: ignore[return-value]
```

- [ ] **Step B2.4: Run tests, expect pass**

Run: `uv run pytest tests/test_gemini.py -v`
Expected: 8 passed.

(No commit.)

## Task B3: `gemini.write_seedance_prompt` — the Pro call with skill MD

**Files:**
- Modify: `app/gemini.py`
- Modify: `tests/test_gemini.py`

- [ ] **Step B3.1: Write failing tests** — APPEND to `tests/test_gemini.py`:

```python
from pathlib import Path


@pytest.mark.asyncio
async def test_write_seedance_prompt_uses_pro_model_and_skill_md(tmp_path):
    skill_md = "DUMMY SKILL CONTENT (instructions about Seedance prompting)"
    skill_path = tmp_path / "skill.md"
    skill_path.write_text(skill_md)

    fake = _mock_genai_client("the optimized seedance prompt goes here.")
    c = GeminiClient(_settings(), _client=fake, skill_md_path=skill_path)

    chunk = ChunkSpec(
        index=0, t_start=0, t_end=15, dialogue="hi",
        action="raises bowl", summary="intro shot",
    )
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian", hair="wavy",
        face="smooth", wardrobe="tee", energy="warm",
    )

    out = await c.write_seedance_prompt(
        chunk=chunk, character=char, framing="selfie",
        prev_summary=None,
    )

    assert out == "the optimized seedance prompt goes here."

    kw = fake.aio.models.generate_content.call_args.kwargs
    assert kw["model"] == GEMINI_PRO_MODEL
    assert kw["config"].system_instruction.startswith("DUMMY SKILL CONTENT")
    # No structured schema — output is the raw prompt text
    assert kw["config"].response_schema is None


@pytest.mark.asyncio
async def test_write_seedance_prompt_includes_prev_summary_when_provided(tmp_path):
    skill_path = tmp_path / "skill.md"; skill_path.write_text("x")
    fake = _mock_genai_client("prompt")
    c = GeminiClient(_settings(), _client=fake, skill_md_path=skill_path)

    chunk = ChunkSpec(index=1, t_start=15, t_end=30, dialogue="d",
                      action="a", summary="s")
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian", hair="x",
        face="y", wardrobe="z", energy="w",
    )

    await c.write_seedance_prompt(
        chunk=chunk, character=char, framing="counter",
        prev_summary="she just picked up the bowl",
    )

    kw = fake.aio.models.generate_content.call_args.kwargs
    assert "she just picked up the bowl" in kw["contents"]
    assert "counter" in kw["contents"]
```

- [ ] **Step B3.2: Run tests, expect fail**

Run: `uv run pytest tests/test_gemini.py -v -k "seedance_prompt"`
Expected: 2 FAIL (no parameter `skill_md_path`).

- [ ] **Step B3.3: Implement** — edit `app/gemini.py`:

  a) Modify `__init__` signature to accept `skill_md_path`:

  ```python
      def __init__(self, settings: Settings, *,
                   skill_md_path: "Path | str | None" = None,
                   _client: Any = None):
          if not settings.gemini_api_key:
              raise RuntimeError(
                  "GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
              )
          if _client is not None:
              self._client = _client
          else:
              from google import genai
              self._client = genai.Client(api_key=settings.gemini_api_key)
          # Lazy-load the skill markdown only when needed.
          self._skill_md_path = skill_md_path
          self._skill_md_cache: str | None = None
  ```

  b) Add a Path import at the top of the file:

  ```python
  from pathlib import Path
  ```

  c) Add at the bottom of `class GeminiClient`:

  ```python
      def _load_skill_md(self) -> str:
          if self._skill_md_cache is not None:
              return self._skill_md_cache
          if self._skill_md_path is None:
              raise RuntimeError("skill_md_path is required for write_seedance_prompt.")
          self._skill_md_cache = Path(self._skill_md_path).read_text()
          return self._skill_md_cache

      async def write_seedance_prompt(
          self, *, chunk: ChunkSpec, character: CharacterDescription,
          framing: FramingChoice, prev_summary: str | None,
      ) -> str:
          skill_md = self._load_skill_md()

          char_block = (
              f"Character (locked across all chunks):\n"
              f"  age: {character.age}, gender: {character.gender}, "
              f"ethnicity: {character.ethnicity}\n"
              f"  hair: {character.hair}\n  face: {character.face}\n"
              f"  wardrobe: {character.wardrobe}\n  energy: {character.energy}"
          )
          continuity_block = (
              f"This is chunk #{chunk.index}.\n"
              + (f"Previous chunk ended with: {prev_summary}\n" if prev_summary
                 else "This is the opening chunk.\n")
              + f"This chunk runs from {chunk.t_start}s to {chunk.t_end}s "
                f"(15-second window).\n"
                f"Dialogue (verbatim): {chunk.dialogue}\n"
                f"Visual action: {chunk.action}\n"
                f"Framing convention: {framing}"
          )
          user_prompt = (
              "Write a final, ready-to-paste Seedance 2.0 video prompt for the chunk below.\n"
              "Apply ALL the rules in the system instruction (three-section structure, "
              "single camera move per slice, anti-distortion fallback, exactly two arms "
              "and two hands, pose lock to prevent 3-arm hallucination).\n"
              "Do NOT include 'pores / peach fuzz / no beauty filter' style cues — use "
              "the affirmative young-skin language from the character description.\n"
              "Return the prompt as plain text only — no commentary, no markdown wrappers.\n\n"
              f"{char_block}\n\n{continuity_block}"
          )
          resp = await self._client.aio.models.generate_content(
              model=GEMINI_PRO_MODEL,
              contents=user_prompt,
              config=self._config(
                  schema=None, system_instruction=skill_md,
                  temperature=0.5, max_output_tokens=4096,
              ),
          )
          return resp.text.strip()
  ```

- [ ] **Step B3.4: Run tests, expect pass**

Run: `uv run pytest tests/test_gemini.py -v`
Expected: 10 passed.

(No commit.)

---

# SUB-PROJECT C — Multi-chunk video chaining

## Task C1: Small mod to `generator.py` — expose `last_frame_url`

**Files:**
- Modify: `app/generator.py`
- Modify: `tests/test_generator.py`

- [ ] **Step C1.1: Write failing test** — APPEND to `tests/test_generator.py`:

```python
def test_last_frame_url_extracted_from_response():
    """If the Seedance response contains content.last_frame_url, it should
    surface on the serialized response so the chain can use it."""
    from app.generator import _serialize
    fake = MagicMock(
        status="succeeded", id="cgt-1",
        content=MagicMock(
            video_url="https://x/v.mp4",
            last_frame_url="https://x/lf.png",
        ),
    )
    out = _serialize(fake)
    assert out["content"]["last_frame_url"] == "https://x/lf.png"
    assert out["content"]["video_url"] == "https://x/v.mp4"
```

- [ ] **Step C1.2: Run, expect pass** — the existing `_serialize` already walks `__dict__`, so this test likely PASSES out of the box.

Run: `uv run pytest tests/test_generator.py::test_last_frame_url_extracted_from_response -v`
Expected: PASS. If it fails (some SDK versions don't expose `last_frame_url` via __dict__), edit `_serialize` to explicitly handle the field — the current implementation should already cover it.

(No commit.)

## Task C2: `app/chain.py` — sequential chunk runner

**Files:**
- Create: `app/chain.py`
- Create: `tests/test_chain.py`

- [ ] **Step C2.1: Write failing tests** in `tests/test_chain.py`

```python
import pytest
from unittest.mock import MagicMock
from app.chain import ChunkPlan, ChunkResult, ChainFailure, run_chain
from app.validation import Settings as FormSettings


def _form_settings():
    return FormSettings(
        model="dreamina-seedance-2-0-260128", ratio="9:16", duration=15,
        resolution="1080p", generate_audio=True, watermark=False,
        return_last_frame=True, seed=None,
    )


def _plan(prompt="hi", first_frame_url=None, is_last=False):
    return ChunkPlan(
        seedance_prompt=prompt, settings=_form_settings(),
        first_frame_url=first_frame_url, is_last=is_last,
    )


def _mock_seedance_client_seq(responses: list[dict]):
    """Each call to create_task → returns (task_id, payload).
    Each call to poll_task_stream → yields (status, final_resp)."""
    client = MagicMock()
    create_call_count = {"n": 0}

    def fake_create(*, prompt, assets, settings):
        i = create_call_count["n"]
        create_call_count["n"] += 1
        return responses[i]["task_id"], {"task_id": responses[i]["task_id"]}

    def fake_stream(task_id):
        # Look up which response this task_id corresponds to
        for resp in responses:
            if resp["task_id"] == task_id:
                yield ("succeeded", resp["final"])
                return
        raise AssertionError(f"unknown task_id {task_id}")

    client.create_task.side_effect = fake_create
    client.poll_task_stream.side_effect = fake_stream
    return client


@pytest.mark.asyncio
async def test_chain_runs_chunks_in_order():
    responses = [
        {"task_id": "cgt-0", "final": MagicMock(
            status="succeeded",
            content=MagicMock(video_url="https://v/0.mp4",
                              last_frame_url="https://lf/0.png"))},
        {"task_id": "cgt-1", "final": MagicMock(
            status="succeeded",
            content=MagicMock(video_url="https://v/1.mp4",
                              last_frame_url="https://lf/1.png"))},
    ]
    client = _mock_seedance_client_seq(responses)
    chunks = [
        _plan(prompt="A", first_frame_url="https://ff/0.png", is_last=False),
        _plan(prompt="B", first_frame_url=None, is_last=True),
    ]

    progress: list = []
    results = await run_chain(
        client=client, chunks=chunks,
        character_ref_url="https://char/ref.png",
        product_ref_url=None,
        on_progress=lambda idx, msg: progress.append((idx, msg)),
    )

    assert [r.video_url for r in results] == ["https://v/0.mp4", "https://v/1.mp4"]
    assert results[0].last_frame_url == "https://lf/0.png"
    # Chunk 0 received the character ref + the explicit first_frame_url
    call0 = client.create_task.call_args_list[0].kwargs
    urls0 = {(a.url, a.role) for a in call0["assets"]}
    assert ("https://char/ref.png", "reference_image") in urls0
    assert ("https://ff/0.png", "first_frame") in urls0
    # Chunk 1 received character ref + chunk 0's last frame
    call1 = client.create_task.call_args_list[1].kwargs
    urls1 = {(a.url, a.role) for a in call1["assets"]}
    assert ("https://char/ref.png", "reference_image") in urls1
    assert ("https://lf/0.png", "first_frame") in urls1


@pytest.mark.asyncio
async def test_chain_threads_product_ref_on_every_chunk():
    responses = [{"task_id": f"cgt-{i}", "final": MagicMock(
        status="succeeded",
        content=MagicMock(video_url=f"v{i}", last_frame_url=f"lf{i}"))}
        for i in range(2)]
    client = _mock_seedance_client_seq(responses)
    chunks = [_plan(first_frame_url="https://ff/0.png"),
              _plan(is_last=True)]
    await run_chain(
        client=client, chunks=chunks,
        character_ref_url="https://char/ref.png",
        product_ref_url="https://prod/p.png",
        on_progress=lambda i, m: None,
    )
    for call in client.create_task.call_args_list:
        urls = {(a.url, a.role) for a in call.kwargs["assets"]}
        assert ("https://prod/p.png", "reference_image") in urls


@pytest.mark.asyncio
async def test_chain_failure_includes_partial_results():
    responses = [
        {"task_id": "cgt-0", "final": MagicMock(
            status="succeeded",
            content=MagicMock(video_url="v0", last_frame_url="lf0"))},
    ]
    client = _mock_seedance_client_seq(responses)

    # Configure the second poll_task_stream call to raise
    original_stream = client.poll_task_stream.side_effect
    def stream_with_failure(task_id):
        if task_id == "cgt-1":
            raise RuntimeError("task failed: simulated boom")
        # delegate to the original for cgt-0
        yield from original_stream(task_id)

    # Need to first call create_task for chunk 1 → returns cgt-1
    create_count = {"n": 0}
    def fake_create(*, prompt, assets, settings):
        i = create_count["n"]; create_count["n"] += 1
        return ["cgt-0", "cgt-1"][i], {"task_id": ["cgt-0", "cgt-1"][i]}
    client.create_task.side_effect = fake_create
    client.poll_task_stream.side_effect = stream_with_failure

    chunks = [_plan(first_frame_url="https://ff/0.png"),
              _plan(is_last=True)]
    with pytest.raises(ChainFailure) as exc:
        await run_chain(
            client=client, chunks=chunks,
            character_ref_url="https://char/ref.png",
            product_ref_url=None,
            on_progress=lambda i, m: None,
        )
    assert exc.value.chunk_index == 1
    assert len(exc.value.partial_results) == 1
    assert exc.value.partial_results[0].video_url == "v0"
```

- [ ] **Step C2.2: Run tests, expect fail**

Run: `uv run pytest tests/test_chain.py -v`
Expected: ImportError.

- [ ] **Step C2.3: Implement** `app/chain.py`:

```python
"""Sequential multi-chunk Seedance runner with continuity.

Each chunk i (i>0) reuses chunk (i-1)'s last_frame as its first_frame, plus
the same character reference image (and optional product reference) on every
chunk.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from app.generator import SeedanceClient, UploadedAsset, _serialize
from app.validation import Settings as FormSettings


@dataclass
class ChunkPlan:
    seedance_prompt: str
    settings: FormSettings
    first_frame_url: str | None     # None means "no explicit first frame"
    is_last: bool                   # if False → request return_last_frame=True


@dataclass
class ChunkResult:
    index: int
    task_id: str
    video_url: str
    last_frame_url: str | None
    raw: dict = field(default_factory=dict)


class ChainFailure(Exception):
    def __init__(self, *, chunk_index: int,
                 partial_results: list[ChunkResult],
                 underlying: BaseException):
        super().__init__(f"chain failed at chunk {chunk_index}: {underlying}")
        self.chunk_index = chunk_index
        self.partial_results = partial_results
        self.underlying = underlying


def _build_assets(
    *, character_ref_url: str,
    product_ref_url: str | None,
    first_frame_url: str | None,
) -> list[UploadedAsset]:
    assets: list[UploadedAsset] = [
        UploadedAsset(url=character_ref_url, kind="image", role="reference_image"),
    ]
    if product_ref_url:
        assets.append(
            UploadedAsset(url=product_ref_url, kind="image", role="reference_image")
        )
    if first_frame_url:
        assets.append(
            UploadedAsset(url=first_frame_url, kind="image", role="first_frame")
        )
    return assets


async def run_chain(
    *, client: SeedanceClient,
    chunks: list[ChunkPlan],
    character_ref_url: str,
    product_ref_url: str | None,
    on_progress: Callable[[int, str], None],
) -> list[ChunkResult]:
    results: list[ChunkResult] = []
    prev_last_frame: str | None = None

    for i, plan in enumerate(chunks):
        first_frame_url = (
            plan.first_frame_url if i == 0 else prev_last_frame
        )
        assets = _build_assets(
            character_ref_url=character_ref_url,
            product_ref_url=product_ref_url,
            first_frame_url=first_frame_url,
        )
        # Force return_last_frame on every chunk except the last.
        settings = type(plan.settings)(
            **{**vars(plan.settings), "return_last_frame": not plan.is_last}
        )

        on_progress(i, f"creating chunk {i + 1}/{len(chunks)} task...")
        try:
            task_id, _payload = client.create_task(
                prompt=plan.seedance_prompt,
                assets=assets, settings=settings,
            )
            on_progress(i, f"chunk {i + 1} task_id={task_id}; polling")

            final = None
            for status, resp in await asyncio.to_thread(
                _collect_stream, client, task_id
            ):
                on_progress(i, f"chunk {i + 1} status={status}")
                if resp is not None:
                    final = resp

            if final is None:
                raise RuntimeError("poll loop exited without a final response")
            serialized = _serialize(final)
            content = (serialized.get("content") or {}) if isinstance(serialized, dict) else {}
            result = ChunkResult(
                index=i, task_id=task_id,
                video_url=content.get("video_url", ""),
                last_frame_url=content.get("last_frame_url"),
                raw=serialized if isinstance(serialized, dict) else {},
            )
            results.append(result)
            prev_last_frame = result.last_frame_url
        except BaseException as e:
            raise ChainFailure(
                chunk_index=i, partial_results=results, underlying=e,
            ) from e

    return results


def _collect_stream(client: SeedanceClient, task_id: str):
    """The Seedance poll_task_stream is sync; collect it to a list so we can
    await it via asyncio.to_thread."""
    return list(client.poll_task_stream(task_id))
```

- [ ] **Step C2.4: Run tests, expect pass**

Run: `uv run pytest tests/test_chain.py -v`
Expected: 3 passed.

(No commit.)

---

# SUB-PROJECT D — Full Workflow + Tab restructure

## Task D1: `app/workflow.py` — state machine + orchestrator (no UI yet)

**Files:**
- Create: `app/workflow.py`
- Create: `tests/test_workflow.py`

- [ ] **Step D1.1: Write failing tests** in `tests/test_workflow.py`

```python
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.workflow import WorkflowSession, save_session, load_session


def test_session_state_transitions(tmp_path):
    sess = WorkflowSession.new(
        brief="b", duration_s=15,
        product_ref_url=None, root=tmp_path,
    )
    assert sess.state == "draft"
    sess.advance_to("script_review")
    assert sess.state == "script_review"


def test_save_load_round_trip(tmp_path):
    sess = WorkflowSession.new(
        brief="b", duration_s=30,
        product_ref_url="https://p/x.png", root=tmp_path,
    )
    sess.state = "asset_review"
    sess.error = "test error"
    save_session(sess)

    loaded = load_session(sess.id, root=tmp_path)
    assert loaded is not None
    assert loaded.brief == "b"
    assert loaded.duration_s == 30
    assert loaded.product_ref_url == "https://p/x.png"
    assert loaded.state == "asset_review"
    assert loaded.error == "test error"


def test_load_session_returns_none_for_missing(tmp_path):
    assert load_session("not-real", root=tmp_path) is None


def test_session_id_is_unique(tmp_path):
    a = WorkflowSession.new(brief="b", duration_s=15,
                            product_ref_url=None, root=tmp_path)
    b = WorkflowSession.new(brief="b", duration_s=15,
                            product_ref_url=None, root=tmp_path)
    assert a.id != b.id
```

- [ ] **Step D1.2: Run, expect fail**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: ImportError.

- [ ] **Step D1.3: Implement** `app/workflow.py`:

```python
"""Workflow session state machine + JSON persistence.

The state machine is owned by the UI layer; this module just stores fields
and (de)serialises them.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

WorkflowState = Literal[
    "draft", "script_review", "generating_assets",
    "asset_review", "generating_videos", "complete", "failed",
]


@dataclass
class WorkflowSession:
    id: str
    state: WorkflowState
    brief: str
    duration_s: int
    product_ref_url: str | None
    created_at: str
    root: Path = field(default_factory=lambda: Path("jobs/workflows"))

    # Filled in along the way; serialised as plain dicts.
    script: dict | None = None         # ScriptDraft as dict
    chunks: list[dict] | None = None   # list[ChunkSpec] as dict
    character: dict | None = None      # CharacterDescription as dict
    framing: str | None = None
    character_ref: dict | None = None  # GeneratedImage as dict
    first_frame: dict | None = None    # GeneratedImage as dict
    seedance_prompts: list[str] | None = None
    chunk_results: list[dict] = field(default_factory=list)  # list[ChunkResult] as dict
    error: str | None = None

    @classmethod
    def new(cls, *, brief: str, duration_s: int,
            product_ref_url: str | None,
            root: Path | str = "jobs/workflows") -> "WorkflowSession":
        return cls(
            id=f"wf-{uuid.uuid4().hex[:12]}",
            state="draft",
            brief=brief,
            duration_s=duration_s,
            product_ref_url=product_ref_url,
            created_at=datetime.now(timezone.utc).isoformat(),
            root=Path(root),
        )

    def advance_to(self, new_state: WorkflowState) -> None:
        self.state = new_state

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["root"] = str(self.root)
        return d


def _path_for(session_id: str, root: Path) -> Path:
    return Path(root) / f"{session_id}.json"


def save_session(session: WorkflowSession) -> None:
    p = _path_for(session.id, session.root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(session.to_dict(), indent=2, default=str))


def load_session(session_id: str, *,
                 root: Path | str = "jobs/workflows") -> WorkflowSession | None:
    p = _path_for(session_id, Path(root))
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    data["root"] = Path(data.get("root", root))
    return WorkflowSession(**data)
```

- [ ] **Step D1.4: Run, expect pass**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: 4 passed.

(No commit.)

## Task D2: Restructure `app/ui.py` into 3 tabs (Tab 2 unchanged, Tab 3 functional, Tab 1 placeholder)

**Files:**
- Modify: `app/ui.py`

- [ ] **Step D2.1: Refactor existing `build_app` into `_build_direct_video_tab`**

Open `app/ui.py`. At the bottom of the file:

  a) Rename the existing `def build_app() -> gr.Blocks:` to `def _build_direct_video_tab() -> None:` and remove the outer `with gr.Blocks(title=...) as app:` wrapper — the function should now register its components into the surrounding gr.Tab context.

  Specifically, replace the lines:

  ```python
  def build_app() -> gr.Blocks:
      with gr.Blocks(title="Seedance 2.0") as app:
          gr.Markdown("# Video-Gen · Seedance 2.0")
          gr.Markdown(...)
  ```

  with:

  ```python
  def _build_direct_video_tab() -> None:
      gr.Markdown(
          "**Tip:** for face-containing references, paste the BytePlus "
          "Seedream URL (or `asset://...` ID) in the URL field — moderation "
          "trusts your own model outputs. Uploaded files are sent through "
          "S3 first and may be rejected if they contain real human faces."
      )
  ```

  Remove the trailing `return app` at the bottom of that function.

  b) Add a new top-level `def build_app()` that builds the three tabs:

  ```python
  def build_app() -> gr.Blocks:
      with gr.Blocks(title="Video-Gen · Seedance 2.0 + Seedream 5.0") as app:
          gr.Markdown("# Video-Gen · Seedance 2.0 + Seedream 5.0")
          with gr.Tabs():
              with gr.Tab("Workflow"):
                  _build_workflow_tab()
              with gr.Tab("Direct video"):
                  _build_direct_video_tab()
              with gr.Tab("Images"):
                  _build_gallery_tab()
      return app


  def _build_workflow_tab() -> None:
      gr.Markdown(
          "Full workflow coming online incrementally. For now, see Direct "
          "video and Images tabs."
      )
  ```

  (Tab 1 implementation lands in Task D4.)

- [ ] **Step D2.2: Add `_build_gallery_tab()` for Tab 3**

Add this function at module level, right above `build_app`:

```python
def _build_gallery_tab() -> None:
    from app.config import load_settings
    from app.gallery import Gallery
    from app.seedream import SeedreamClient, DEFAULT_SIZE

    gallery_store = Gallery()

    with gr.Row():
        with gr.Column(scale=1):
            prompt = gr.Textbox(label="Prompt", lines=5,
                                placeholder="Describe the image you want...")
            size = gr.Dropdown(
                ["1440x2560", "2560x1440", "1920x1920", "1920x2160"],
                value=DEFAULT_SIZE, label="Size",
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
            current_url = gr.Textbox(label="URL (24h)", interactive=False)
            current_prompt = gr.Textbox(label="Prompt used",
                                        interactive=False, lines=3)
            grid = gr.Gallery(label="All generated images",
                              columns=4, height=480, allow_preview=True)

    def _refresh_grid(source: str):
        rows = gallery_store.list(
            source=None if source == "all" else source,
        )
        return [(row.url, row.prompt[:80]) for row in rows]

    def _generate(prompt_str: str, size_str: str, n_int: int,
                  source_str: str):
        try:
            settings = load_settings()
        except RuntimeError as e:
            yield "", "", "", _refresh_grid(source_str), f"CONFIG ERROR: {e}"
            return
        client = SeedreamClient(settings)
        yield "", "", "", _refresh_grid(source_str), "calling Seedream..."
        try:
            results = client.generate_sync(
                prompt=prompt_str, size=size_str, n=int(n_int),
                source="tab3",
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
        inputs=[prompt, size, n, source_filter],
        outputs=[current, current_url, current_prompt, grid, log],
    )
    source_filter.change(
        lambda s: _refresh_grid(s),
        inputs=[source_filter], outputs=[grid],
    )
    # Load existing gallery on tab open.
    grid.value = _refresh_grid("all")
```

- [ ] **Step D2.3: Smoke-import the rebuilt UI**

Run: `ARK_API_KEY=dummy GEMINI_API_KEY=dummy uv run python -c "from app.ui import build_app; build_app(); print('ok')"`
Expected: prints `ok`.

- [ ] **Step D2.4: Run the full test suite to confirm nothing broke**

Run: `uv run pytest -v`
Expected: all previously passing tests still pass.

(No commit.)

## Task D3: Wire the orchestrator (Gemini → Seedream → user gate → Seedance chain)

**Files:**
- Modify: `app/ui.py`

- [ ] **Step D3.1: Replace the placeholder `_build_workflow_tab()` with the wizard**

Open `app/ui.py`, replace the `_build_workflow_tab()` placeholder with:

```python
def _build_workflow_tab() -> None:
    from app.config import load_settings
    from app.gallery import Gallery
    from app.gemini import GeminiClient, ScriptDraft, ChunkSpec, CharacterDescription
    from app.seedream import SeedreamClient
    from app.generator import make_client as make_seedance_client
    from app.workflow import WorkflowSession, save_session
    from app.chain import ChunkPlan, run_chain, ChainFailure
    from app.validation import Settings as FormSettings
    from pathlib import Path
    import asyncio

    gallery_store = Gallery()

    session_state = gr.State(None)  # holds the WorkflowSession dict

    gr.Markdown(
        "### Full Workflow (Brief → Script → Assets → Videos)\n"
        "Two approval gates: edit the draft script, then review the asset "
        "bundle. Chunks generate sequentially with character + last-frame "
        "continuity."
    )

    with gr.Group():
        gr.Markdown("**Step 1 · Brief**")
        brief = gr.Textbox(label="Brief", lines=4,
                           placeholder="e.g., 15s UGC ad for Goat Life Oats...")
        duration = gr.Slider(4, 60, value=15, step=1, label="Duration (s)")
        product_url = gr.Textbox(label="Product reference URL (optional)",
                                 placeholder="https://... (BytePlus URL or asset://id)")
        draft_btn = gr.Button("Draft script", variant="primary")
        log = gr.Textbox(label="Log", lines=10, interactive=False,
                         autoscroll=True)

    with gr.Group(visible=False) as script_panel:
        gr.Markdown("**Step 2 · Script review** (edit if needed)")
        script_box = gr.Textbox(label="Script", lines=8)
        tone_box = gr.Textbox(label="Tone notes", lines=2)
        chunks_count = gr.Number(label="Chunks (auto)", interactive=False)
        approve_script_btn = gr.Button("Approve & generate assets",
                                       variant="primary")

    with gr.Group(visible=False) as assets_panel:
        gr.Markdown("**Step 3 · Asset bundle review**")
        char_img = gr.Image(label="Character reference", interactive=False)
        first_img = gr.Image(label="First frame", interactive=False)
        prompts_box = gr.Textbox(
            label="Seedance prompts (one per chunk, separated by `=== CHUNK N ===`)",
            lines=20, max_lines=40,
        )
        approve_assets_btn = gr.Button("Approve & generate videos",
                                       variant="primary")

    with gr.Group(visible=False) as result_panel:
        gr.Markdown("**Step 4 · Generated videos**")
        videos_md = gr.Markdown("No videos yet.")
        videos_json = gr.Code(label="Chunk results JSON", language="json")

    def _async_run(coro):
        """Run an async coroutine from sync Gradio handler."""
        return asyncio.run(coro)

    def _log_append(buf: str, line: str) -> str:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        return (buf or "") + f"[{ts}] {line}\n"

    def on_draft(brief_str, duration_int, product_str, sess):
        if not brief_str.strip():
            return ("", "", 0, gr.update(visible=False),
                    sess, "ERROR: brief is required")
        try:
            settings = load_settings()
        except RuntimeError as e:
            return ("", "", 0, gr.update(visible=False), sess,
                    f"CONFIG ERROR: {e}")

        sess_obj = WorkflowSession.new(
            brief=brief_str, duration_s=int(duration_int),
            product_ref_url=product_str.strip() or None,
        )
        save_session(sess_obj)

        gem = GeminiClient(settings,
                           skill_md_path=Path("video-seedream-skill.md"))
        log_buf = _log_append("", f"workflow {sess_obj.id} created")
        log_buf = _log_append(log_buf, "calling Gemini to draft script...")

        try:
            draft = _async_run(
                gem.generate_script(brief=brief_str,
                                    duration_s=int(duration_int))
            )
        except Exception as e:
            return ("", "", 0, gr.update(visible=False),
                    sess_obj.to_dict(),
                    _log_append(log_buf, f"GEMINI ERROR: {e}"))

        sess_obj.script = draft.model_dump()
        sess_obj.advance_to("script_review")
        save_session(sess_obj)
        log_buf = _log_append(log_buf,
                              f"script drafted ({len(draft.full_script.split())} words, "
                              f"{draft.n_chunks} chunks)")
        return (
            draft.full_script, draft.tone_notes, draft.n_chunks,
            gr.update(visible=True),
            sess_obj.to_dict(), log_buf,
        )

    draft_btn.click(
        on_draft,
        inputs=[brief, duration, product_url, session_state],
        outputs=[script_box, tone_box, chunks_count,
                 script_panel, session_state, log],
    )

    def on_approve_script(script_str, tone_str, n_chunks_int,
                          sess_dict, log_buf):
        if not sess_dict:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, "ERROR: no session"))
        settings = load_settings()
        gem = GeminiClient(settings,
                           skill_md_path=Path("video-seedream-skill.md"))
        seedr = SeedreamClient(settings)

        # Persist edited script.
        sess_dict["script"] = {
            "full_script": script_str,
            "tone_notes": tone_str,
            "n_chunks": int(n_chunks_int),
        }
        sess_dict["state"] = "generating_assets"
        log_buf = _log_append(log_buf, "running parallel Gemini calls...")

        draft = ScriptDraft.model_validate(sess_dict["script"])

        async def do_all():
            chunks, character, framing = await asyncio.gather(
                gem.split_script(script=draft, n_chunks=draft.n_chunks),
                gem.describe_character(brief=sess_dict["brief"], script=draft),
                gem.pick_framing(brief=sess_dict["brief"], script=draft),
            )
            return chunks, character, framing

        try:
            chunks, character, framing = _async_run(do_all())
        except Exception as e:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, f"GEMINI ERROR: {e}"))

        sess_dict["chunks"] = [c.model_dump() for c in chunks]
        sess_dict["character"] = character.model_dump()
        sess_dict["framing"] = framing
        log_buf = _log_append(log_buf,
                              f"split={len(chunks)} chunks, framing={framing}")

        # Generate character + first-frame images via Seedream (parallel).
        async def gen_images():
            char_prompt = _char_image_prompt(character)
            ff_prompt = _first_frame_prompt(character, chunks[0], framing,
                                            sess_dict.get("product_ref_url"))
            char_imgs, ff_imgs = await asyncio.gather(
                asyncio.to_thread(seedr.generate_sync, prompt=char_prompt,
                                  source="workflow"),
                asyncio.to_thread(seedr.generate_sync, prompt=ff_prompt,
                                  source="workflow"),
            )
            return char_imgs[0], ff_imgs[0]

        try:
            log_buf = _log_append(log_buf,
                                  "generating character ref + first frame...")
            char_img_obj, first_img_obj = _async_run(gen_images())
        except Exception as e:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, f"SEEDREAM ERROR: {e}"))

        gallery_store.add(char_img_obj)
        gallery_store.add(first_img_obj)
        sess_dict["character_ref"] = char_img_obj.to_dict()
        sess_dict["first_frame"] = first_img_obj.to_dict()
        log_buf = _log_append(log_buf, "images saved to gallery")

        # Generate one Seedance prompt per chunk (sequential, Pro model).
        async def gen_prompts():
            out = []
            prev_summary = None
            for c in chunks:
                p = await gem.write_seedance_prompt(
                    chunk=c, character=character, framing=framing,
                    prev_summary=prev_summary,
                )
                out.append(p)
                prev_summary = c.summary
            return out

        try:
            log_buf = _log_append(log_buf,
                                  "writing Seedance prompts via Gemini Pro...")
            prompts = _async_run(gen_prompts())
        except Exception as e:
            return (None, None, "", gr.update(visible=False),
                    sess_dict, _log_append(log_buf, f"GEMINI ERROR: {e}"))

        sess_dict["seedance_prompts"] = prompts
        sess_dict["state"] = "asset_review"
        sess_obj = WorkflowSession(**{k: v for k, v in sess_dict.items()
                                      if k in WorkflowSession.__dataclass_fields__})
        save_session(sess_obj)

        joined = "\n\n".join(
            f"=== CHUNK {i} ===\n{p}" for i, p in enumerate(prompts)
        )
        log_buf = _log_append(log_buf, f"asset bundle ready ({len(prompts)} prompts)")
        return (
            char_img_obj.url, first_img_obj.url, joined,
            gr.update(visible=True),
            sess_dict, log_buf,
        )

    approve_script_btn.click(
        on_approve_script,
        inputs=[script_box, tone_box, chunks_count, session_state, log],
        outputs=[char_img, first_img, prompts_box,
                 assets_panel, session_state, log],
    )

    def on_approve_assets(prompts_text, sess_dict, log_buf):
        if not sess_dict:
            return ("ERROR: no session", "", gr.update(visible=False),
                    sess_dict, log_buf)
        # Parse the (possibly edited) prompts back out.
        parts = [
            p.strip() for p in prompts_text.split("=== CHUNK ")
            if p.strip()
        ]
        prompts = []
        for part in parts:
            # The number followed by " ===\n..."
            try:
                _, rest = part.split("===\n", 1)
            except ValueError:
                rest = part
            prompts.append(rest.strip())

        settings = load_settings()
        ark = make_seedance_client(settings)
        log_buf = _log_append(log_buf,
                              f"running Seedance chain ({len(prompts)} chunks)...")

        plans = [
            ChunkPlan(
                seedance_prompt=prompts[i],
                settings=FormSettings(
                    model="dreamina-seedance-2-0-260128",
                    ratio="9:16", duration=15, resolution="1080p",
                    generate_audio=True, watermark=False,
                    return_last_frame=(i < len(prompts) - 1),
                    seed=None,
                ),
                first_frame_url=(sess_dict["first_frame"]["url"] if i == 0 else None),
                is_last=(i == len(prompts) - 1),
            )
            for i in range(len(prompts))
        ]

        progress_buf = {"v": log_buf}

        def on_progress(idx, msg):
            progress_buf["v"] = _log_append(progress_buf["v"],
                                            f"chunk {idx + 1}: {msg}")

        try:
            results = _async_run(run_chain(
                client=ark, chunks=plans,
                character_ref_url=sess_dict["character_ref"]["url"],
                product_ref_url=sess_dict.get("product_ref_url"),
                on_progress=on_progress,
            ))
        except ChainFailure as e:
            progress_buf["v"] = _log_append(
                progress_buf["v"],
                f"FAILED at chunk {e.chunk_index + 1}: {e.underlying}",
            )
            sess_dict["chunk_results"] = [
                {"index": r.index, "video_url": r.video_url,
                 "last_frame_url": r.last_frame_url, "task_id": r.task_id}
                for r in e.partial_results
            ]
            sess_dict["state"] = "failed"
            sess_dict["error"] = str(e.underlying)
            return (
                _videos_md(e.partial_results),
                json.dumps(sess_dict["chunk_results"], indent=2),
                gr.update(visible=True),
                sess_dict, progress_buf["v"],
            )

        sess_dict["chunk_results"] = [
            {"index": r.index, "video_url": r.video_url,
             "last_frame_url": r.last_frame_url, "task_id": r.task_id}
            for r in results
        ]
        sess_dict["state"] = "complete"
        return (
            _videos_md(results),
            json.dumps(sess_dict["chunk_results"], indent=2),
            gr.update(visible=True),
            sess_dict,
            _log_append(progress_buf["v"], f"DONE · {len(results)} videos"),
        )

    approve_assets_btn.click(
        on_approve_assets,
        inputs=[prompts_box, session_state, log],
        outputs=[videos_md, videos_json,
                 result_panel, session_state, log],
    )
```

Then add these small helpers at module level (near `_build_workflow_tab`):

```python
def _char_image_prompt(c) -> str:
    return (
        f"Full-body hyperrealistic portrait of a {c.age}-year-old "
        f"{c.ethnicity} {c.gender} content creator with {c.hair}. "
        f"{c.face}. Wearing {c.wardrobe}. {c.energy} energy. "
        f"Vertical 9:16 framing, neutral light backdrop, soft natural "
        f"window light. Photogenic Instagram lifestyle vlogger aesthetic. "
        f"4K HD, sharp focus on the face, smooth healthy young skin, "
        f"no aged or weathered look, no overprocessed AI smoothness."
    )


def _first_frame_prompt(c, chunk, framing, product_ref_url) -> str:
    framing_desc = {
        "selfie": "tight handheld arm-extended selfie medium close-up",
        "tripod": "static medium shot from a tripod, hands free",
        "counter": "behind-the-counter static shot looking slightly down",
        "walking": "walking-and-talking handheld medium shot",
    }[framing]
    product_clause = (
        f" The product visible: {product_ref_url}." if product_ref_url
        else ""
    )
    return (
        f"Hyperrealistic UGC opening frame, {framing_desc}. "
        f"A {c.age}-year-old {c.ethnicity} {c.gender} with {c.hair} and "
        f"{c.face}, wearing {c.wardrobe}, {c.energy} energy. "
        f"Visual action in this opening: {chunk.action}.{product_clause} "
        f"Vertical 9:16, soft natural window light, 4K HD, smooth healthy "
        f"young skin, no overprocessed AI smoothness."
    )


def _videos_md(results) -> str:
    lines = [f"### {len(results)} chunk(s) generated\n"]
    for r in results:
        lines.append(f"**Chunk {r.index}** — [video]({r.video_url})\n")
    return "\n".join(lines)
```

Add `import json` at the top of `app/ui.py` if not already there.

- [ ] **Step D3.2: Smoke-import the workflow tab**

Run: `ARK_API_KEY=dummy GEMINI_API_KEY=dummy uv run python -c "from app.ui import build_app; build_app(); print('ok')"`
Expected: prints `ok` (Gradio compiles all three tabs).

- [ ] **Step D3.3: Run all tests**

Run: `uv run pytest -v`
Expected: all tests pass (no UI tests; the prior test suite still passes).

(No commit.)

---

# FINAL — manual smoke tests

These need real API credentials and cost real money. Document the checklist so the user can run them.

## Task SMOKE: end-to-end runs

- [ ] **Set up `.env`** — ensure ARK_API_KEY, GEMINI_API_KEY, AWS_* are populated. Confirm `video-seedream-skill.md` exists in the project root.

- [ ] **Run the app**

Run: `uv run python main.py`
Expected: Gradio prints `Running on local URL: http://127.0.0.1:7860`. Open it. Three tabs visible.

- [ ] **Tab 3 (Images) smoke test**

In Tab 3:
- Prompt: `"Indian woman, 24, lifestyle vlogger holding a bowl of oats, hyperrealistic, kitchen, soft morning light, 9:16"`
- Size: `1440x2560`, n: `1`
- Click Generate.

Expected: image appears in "Most recent", URL is shown, prompt repeated, the grid below adds the new thumbnail. `jobs/images.json` exists and contains one entry.

- [ ] **Tab 2 (Direct video) smoke test** — unchanged from v1; confirm it still works the same.

- [ ] **Tab 1 (Workflow) smoke test — 15s ad**

In Tab 1:
- Brief: `"15s UGC ad for Goat Life high-protein oats: hook about why most people fail their health goals, pivot to fixing breakfast, three benefit numbers (20g protein, 30s prep, tasty), CTA: start small."`
- Duration: `15`
- Product URL: leave empty
- Click "Draft script".

Expected:
  1. Log shows `workflow wf-... created`, then `script drafted (~50 words, 1 chunks)`.
  2. The script-review panel appears with the draft. Tone notes filled in. n_chunks = 1.
  3. Click "Approve & generate assets". Log shows: parallel Gemini calls running, then character + first-frame Seedream calls, then `writing Seedance prompts via Gemini Pro`.
  4. Asset panel shows character ref image, first-frame image, and one chunk's Seedance prompt.
  5. Click "Approve & generate videos". Log shows Seedance chunk polling. Result panel appears with one chunk's video link.
  6. `jobs/workflows/wf-*.json` exists with full state.
  7. The two images are also visible in Tab 3 with `source="workflow"`.

- [ ] **Tab 1 smoke test — 30s ad** (2 chunks)

Same brief, duration `30`. Expected: 2 chunks in the asset panel, 2 video URLs in result. Continuity: open chunk 1's video, note the final frame; open chunk 2's video, note that the first frame matches.

---

## Self-review (already applied)

- **Spec coverage:** A=Task A1+A2, B=Task B1+B2+B3, C=Task C1+C2, D=Task D1+D2+D3. Tabs created in D2. Gallery unification in D3. Continuity logic in C2. Failure handling in chain + workflow handlers. Two approval gates in D3 (script-review and asset-review panels).
- **Placeholders:** none — every step has actual code or actual commands.
- **Type consistency:** `ScriptDraft`/`ChunkSpec`/`CharacterDescription` defined in B1, used in B2/B3/D3 with same field names. `GeneratedImage` defined in A1, used in A2/D3 with same shape. `ChunkPlan`/`ChunkResult`/`ChainFailure` defined in C2 and used in D3.
- **No commits:** every "Commit" step from the standard template is omitted per user constraint.
