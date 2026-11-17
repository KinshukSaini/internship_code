"""Pure form-validation rules. No I/O."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AssetKind = Literal["image", "video", "audio"]


@dataclass(frozen=True)
class Asset:
    local_path: str
    kind: AssetKind
    role: str


@dataclass(frozen=True)
class Settings:
    """Per-request video generation settings."""
    model: str                  # e.g. "seedance-2.0-image-to-video"
    ratio: str                  # aspect ratio: "9:16" | "16:9" | ...
    duration: int               # seconds, 4-15
    resolution: str             # "480p" | "720p" | "1080p"
    generate_audio: bool
    return_last_frame: bool     # kept for compat; EvoLink ignores it
    seed: int | None


class ValidationError(ValueError):
    """User-facing form error. Message is shown verbatim in the UI."""


MAX_IMAGES = 9
MAX_VIDEOS = 3
MAX_AUDIOS = 3
DURATION_MIN, DURATION_MAX = 4, 15


def validate_form(*, prompt: str, assets: list[Asset], settings: Settings) -> None:
    if not prompt or not prompt.strip():
        raise ValidationError("prompt is required.")

    images = [a for a in assets if a.kind == "image"]
    videos = [a for a in assets if a.kind == "video"]
    audios = [a for a in assets if a.kind == "audio"]

    if len(images) > MAX_IMAGES:
        raise ValidationError(f"at most {MAX_IMAGES} images allowed (got {len(images)}).")
    if len(videos) > MAX_VIDEOS:
        raise ValidationError(f"at most {MAX_VIDEOS} videos allowed (got {len(videos)}).")
    if len(audios) > MAX_AUDIOS:
        raise ValidationError(f"at most {MAX_AUDIOS} audios allowed (got {len(audios)}).")

    first_frames = [a for a in images if a.role == "first_frame"]
    last_frames = [a for a in images if a.role == "last_frame"]
    ref_images = [a for a in images if a.role == "reference_image"]

    if len(first_frames) > 1:
        raise ValidationError("only one image may have role first_frame.")
    if len(last_frames) > 1:
        raise ValidationError("only one image may have role last_frame.")
    if (first_frames or last_frames) and ref_images:
        raise ValidationError(
            "cannot mix first_frame/last_frame with reference_image; "
            "use one or the other."
        )

    if not (DURATION_MIN <= settings.duration <= DURATION_MAX):
        raise ValidationError(
            f"duration must be between {DURATION_MIN} and {DURATION_MAX} seconds."
        )

    # EvoLink's fast variant caps at 720p.
    if "-fast-" in settings.model and settings.resolution == "1080p":
        raise ValidationError("Seedance 2.0 Fast does not support 1080p resolution.")

    if not images and not videos and audios:
        raise ValidationError("text + audio only is not supported by the API.")
