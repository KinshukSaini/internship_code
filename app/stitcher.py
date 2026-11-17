"""ffmpeg-based lossless mp4 concat for stitching workflow chunks.

Falls back gracefully if ffmpeg isn't installed — the workflow still produces
the individual chunk videos.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

# EvoLink's CDN (files.evolink.ai) denies requests with the default
# `Python-urllib/X.Y` User-Agent (verified 2026-05-26: bare curl, browser
# UA, and even an empty UA all return 200, but Python-urllib returns 403).
# Spoof a generic browser UA so the download succeeds.
_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def download(url: str, dest: Path) -> None:
    """Download a URL to a local file. Creates parent dirs."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers=_DOWNLOAD_HEADERS)
    with urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())


def stitch(video_paths: list[Path], output_path: Path) -> None:
    """Concat videos losslessly via ffmpeg's concat demuxer.

    Requires all inputs to share codec + dimensions + fps — Seedance outputs
    do, since every chunk is generated with the same settings.
    """
    if not has_ffmpeg():
        raise RuntimeError(
            "ffmpeg not found in PATH; install via `brew install ffmpeg` "
            "(macOS) or `apt install ffmpeg` (Linux)."
        )
    if not video_paths:
        raise ValueError("no videos to stitch")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for p in video_paths:
            f.write(f"file '{Path(p).resolve()}'\n")
        manifest = Path(f.name)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(manifest), "-c", "copy", str(output_path)],
            check=True, capture_output=True,
        )
    finally:
        manifest.unlink(missing_ok=True)
