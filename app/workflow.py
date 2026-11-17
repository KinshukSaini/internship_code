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
    script: dict | None = None
    screenplay: dict | None = None        # v2.2: written before split_script
    chunks: list[dict] | None = None
    character: dict | None = None
    framing: str | None = None            # kept for back-compat; per-chunk
                                          # framing now lives on each ChunkSpec
    character_ref: dict | None = None
    first_frame: dict | None = None
    seedance_prompts: list[str] | None = None
    chunk_results: list[dict] = field(default_factory=list)
    error: str | None = None
    # v2.1 additions
    start_with_product: bool = False              # resolved value
    start_with_product_mode: str = "auto"         # "auto" | "force_on" | "force_off"
    use_video_continuity: bool = True
    chunk_local_paths: list[str] = field(default_factory=list)
    stitched_video_path: str | None = None
    # Phase 3 (FLF2V): N+1 Nano-Banana-generated anchor URLs, where each
    # consecutive pair (i, i+1) becomes a chunk's first_frame/last_frame.
    # Persisted so the session is replayable and inspectable.
    anchor_urls: list[str] = field(default_factory=list)

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
    # Forward-compat: only pass known fields so old sessions don't crash if
    # we add or remove fields between versions.
    known = WorkflowSession.__dataclass_fields__
    return WorkflowSession(**{k: v for k, v in data.items() if k in known})
