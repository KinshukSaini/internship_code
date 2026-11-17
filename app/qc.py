"""Gemini multimodal QC of the finished ad — an AI reviewer, REPORT-ONLY.

Design follows the July-2026 judge research: a defect-audit rubric (not a
persona), coarse 0-2 dimension scores (fine scales destabilise LLM judges),
a mandatory ``evidence`` field per issue (the best hallucination brake),
timestamp RANGES (LLM timestamps are hints, not ground truth), and an
explicit encoding-artifact vs generation-defect split (judges without that
distinction can score artifact-laden content HIGHER). The report is stored
and shown, never auto-applied — closed-loop correction is only sound for
deterministic knobs, and even then needs a re-measure step this pipeline
doesn't have yet.

The whole video goes up via the Files API at 2 fps sampling (1 fps default
aliases past sub-second lipsync/motion glitches) plus three full-resolution
keyframes as image parts — images bypass the lossy video path and give the
model real pixels for colour/exposure judgment.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from app.config import GEMINI_PRO_MODEL, Settings

_REVIEW_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "issues": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "category": {"type": "STRING", "enum": [
                        "color", "skin", "exposure", "captions", "framing",
                        "generation_artifact", "encoding_artifact",
                        "lipsync", "pacing", "audio", "product",
                    ]},
                    "severity": {"type": "INTEGER"},
                    "timestamp": {"type": "STRING"},
                    "evidence": {"type": "STRING"},
                    "issue": {"type": "STRING"},
                    "suggested_fix": {"type": "STRING"},
                    "confidence": {"type": "STRING",
                                   "enum": ["low", "medium", "high"]},
                },
                "required": ["category", "severity", "timestamp", "evidence",
                             "issue", "suggested_fix", "confidence"],
            },
        },
        "scores": {
            "type": "OBJECT",
            "properties": {
                "visual_quality": {"type": "INTEGER"},
                "color_consistency": {"type": "INTEGER"},
                "caption_quality": {"type": "INTEGER"},
                "hook_strength": {"type": "INTEGER"},
                "overall": {"type": "INTEGER"},
            },
            "required": ["visual_quality", "color_consistency",
                         "caption_quality", "hook_strength", "overall"],
        },
        "verdict": {"type": "STRING", "enum": ["ship", "fix", "reject"]},
    },
    "required": ["issues", "scores", "verdict"],
}

_REVIEW_PROMPT = """\
You are auditing a finished 9:16 short-form UGC ad (AI-generated creator \
footage, burned captions) for DEFECTS before it ships to Instagram Reels / \
TikTok. Do NOT comment on strengths and do NOT praise. If the ad is clean, \
say so by returning few/no issues — but look hard first; find at least 3 \
timestamped problems if they exist.

Audit each dimension:
- color: shot-to-shot colour/exposure jumps at cuts, casts (esp. \
green/magenta in skin), over-grading, blown highlights, banding
- skin: plastic/AI-looking skin, waxy texture, unnatural tone
- captions: timing vs speech, position (safe zones), legibility, typos, \
words popping off-voice
- framing: subject cropped badly, product not readable, dead space
- generation_artifact: warped hands/teeth/eyes, morphing objects, \
background instability, identity drift between shots (report separately \
from encoding_artifact = blockiness/ringing from compression)
- lipsync: mouth not matching audio (give the exact seconds)
- pacing: dead air, beats overstaying, abrupt cut-offs
- audio: level jumps between beats, harsh sibilance, dropouts
- product: label illegible, product floating/ungrounded, wrong-looking \
packaging

Rules:
- severity: 1 nitpick … 5 unshippable.
- timestamp: a RANGE like "00:07-00:09" (never a single frame).
- evidence: describe ONLY what is literally visible/audible at that \
timestamp — if you cannot describe concrete evidence, do not report the \
issue.
- suggested_fix: one concrete, actionable sentence.
- The three attached still images are full-resolution keyframes — use them \
for colour/exposure/skin judgment; use the video for motion/sync/pacing.
- scores: 0 = fails, 1 = acceptable, 2 = good. verdict: ship (no severity \
≥3), fix (fixable severity ≥3), reject (unshippable).
"""


def _keyframes(video_path: str | Path, duration: float | None) -> list[bytes]:
    """Three full-res PNG keyframes (15/50/85%) for real-pixel judgment."""
    dur = duration or 20.0
    frames: list[bytes] = []
    for pct in (0.15, 0.5, 0.85):
        try:
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{dur * pct:.2f}",
                 "-i", str(video_path), "-frames:v", "1",
                 "-f", "image2", "-c:v", "png", "-"],
                capture_output=True, check=True,
            ).stdout
            if out:
                frames.append(out)
        except Exception:
            continue
    return frames


async def review_video(video_path: str | Path, settings: Settings, *,
                       duration: float | None = None,
                       model: str = GEMINI_PRO_MODEL) -> dict:
    """Run the Gemini defect audit over the finished ad; returns the report.

    Raises on failure — the CALLER decides that QC is best-effort (the
    pipeline wraps this and never fails a paid render on a review error).
    """
    from google import genai  # noqa: PLC0415 — lazy SDK import (convention)
    from google.genai import types  # noqa: PLC0415

    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded = await asyncio.to_thread(
        client.files.upload, file=str(video_path))
    try:
        deadline = time.monotonic() + 120
        while getattr(uploaded.state, "name", uploaded.state) == "PROCESSING":
            if time.monotonic() > deadline:
                raise RuntimeError("Gemini file processing timed out")
            await asyncio.sleep(3)
            uploaded = await asyncio.to_thread(
                client.files.get, name=uploaded.name)
        if getattr(uploaded.state, "name", uploaded.state) != "ACTIVE":
            raise RuntimeError(f"Gemini file state: {uploaded.state}")

        parts: list[Any] = [
            types.Part(
                file_data=types.FileData(file_uri=uploaded.uri,
                                         mime_type=uploaded.mime_type),
                video_metadata=types.VideoMetadata(fps=2),
            ),
        ]
        for png in _keyframes(video_path, duration):
            parts.append(types.Part.from_bytes(data=png, mime_type="image/png"))
        parts.append(types.Part(text=_REVIEW_PROMPT))

        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_REVIEW_SCHEMA,
            ),
        )
        return json.loads(resp.text)
    finally:
        try:
            await asyncio.to_thread(client.files.delete, name=uploaded.name)
        except Exception:
            pass


def summarize(report: dict) -> str:
    """One-glance text summary for progress logs."""
    issues = report.get("issues") or []
    sev3 = [i for i in issues if i.get("severity", 0) >= 3]
    scores = report.get("scores") or {}
    return (
        f"AI review: verdict={report.get('verdict')} "
        f"overall={scores.get('overall')}/2 — {len(issues)} issue(s), "
        f"{len(sev3)} at severity ≥3"
        + (f"; top: [{sev3[0]['category']} {sev3[0]['timestamp']}] "
           f"{sev3[0]['issue']}" if sev3 else "")
    )
