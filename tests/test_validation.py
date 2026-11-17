import pytest
from app.validation import (
    Asset,
    Settings as FormSettings,
    validate_form,
    ValidationError,
)


def _basic_settings(**overrides):
    base = dict(
        model="seedance-2.0-image-to-video",
        ratio="9:16",
        duration=8,
        resolution="720p",
        generate_audio=True,
        return_last_frame=False,
        seed=None,
    )
    base.update(overrides)
    return FormSettings(**base)


def test_empty_prompt_rejected():
    with pytest.raises(ValidationError, match="prompt"):
        validate_form(prompt="   ", assets=[], settings=_basic_settings())


def test_too_many_images_rejected():
    assets = [Asset(local_path=f"/tmp/i{i}.png", kind="image", role="reference_image")
              for i in range(10)]
    with pytest.raises(ValidationError, match="9 images"):
        validate_form(prompt="hi", assets=assets, settings=_basic_settings())


def test_too_many_videos_rejected():
    assets = [Asset(local_path=f"/tmp/v{i}.mp4", kind="video", role="reference_video")
              for i in range(4)]
    with pytest.raises(ValidationError, match="3 videos"):
        validate_form(prompt="hi", assets=assets, settings=_basic_settings())


def test_too_many_audios_rejected():
    assets = [Asset(local_path=f"/tmp/a{i}.mp3", kind="audio", role="reference_audio")
              for i in range(4)]
    with pytest.raises(ValidationError, match="3 audios"):
        validate_form(prompt="hi", assets=assets, settings=_basic_settings())


def test_two_first_frames_rejected():
    assets = [
        Asset(local_path="/tmp/a.png", kind="image", role="first_frame"),
        Asset(local_path="/tmp/b.png", kind="image", role="first_frame"),
    ]
    with pytest.raises(ValidationError, match="first_frame"):
        validate_form(prompt="hi", assets=assets, settings=_basic_settings())


def test_first_frame_with_reference_image_rejected():
    assets = [
        Asset(local_path="/tmp/a.png", kind="image", role="first_frame"),
        Asset(local_path="/tmp/b.png", kind="image", role="reference_image"),
    ]
    with pytest.raises(ValidationError, match="cannot mix"):
        validate_form(prompt="hi", assets=assets, settings=_basic_settings())


def test_duration_out_of_range_rejected():
    with pytest.raises(ValidationError, match="duration"):
        validate_form(prompt="hi", assets=[],
                      settings=_basic_settings(duration=16))


def test_fast_model_with_1080p_rejected():
    with pytest.raises(ValidationError, match="1080p"):
        validate_form(
            prompt="hi", assets=[],
            settings=_basic_settings(
                model="seedance-2.0-fast-image-to-video",
                resolution="1080p",
            ),
        )


def test_audio_only_rejected():
    assets = [Asset(local_path="/tmp/a.mp3", kind="audio", role="reference_audio")]
    with pytest.raises(ValidationError, match=r"text \+ audio"):
        validate_form(prompt="hi", assets=assets, settings=_basic_settings())


def test_happy_path_text_only():
    validate_form(prompt="a daisy field", assets=[], settings=_basic_settings())


def test_happy_path_text_plus_image():
    assets = [Asset(local_path="/tmp/a.png", kind="image", role="first_frame")]
    validate_form(prompt="hi", assets=assets, settings=_basic_settings())
