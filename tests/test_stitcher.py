from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from app.stitcher import has_ffmpeg, stitch, download


def test_has_ffmpeg_returns_true_when_found():
    with patch("app.stitcher.shutil.which", return_value="/usr/bin/ffmpeg"):
        assert has_ffmpeg() is True


def test_has_ffmpeg_returns_false_when_not_found():
    with patch("app.stitcher.shutil.which", return_value=None):
        assert has_ffmpeg() is False


def test_stitch_raises_when_ffmpeg_missing(tmp_path):
    with patch("app.stitcher.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            stitch([tmp_path / "a.mp4"], tmp_path / "out.mp4")


def test_stitch_raises_on_empty_input(tmp_path):
    with patch("app.stitcher.shutil.which", return_value="/usr/bin/ffmpeg"):
        with pytest.raises(ValueError, match="no videos"):
            stitch([], tmp_path / "out.mp4")


def test_stitch_calls_ffmpeg_with_concat_demuxer(tmp_path):
    a = tmp_path / "a.mp4"; a.write_bytes(b"x")
    b = tmp_path / "b.mp4"; b.write_bytes(b"y")
    out = tmp_path / "out.mp4"

    with patch("app.stitcher.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("app.stitcher.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        stitch([a, b], out)

    args = mock_run.call_args.args[0]
    assert args[0] == "ffmpeg"
    assert "-f" in args and "concat" in args
    assert "-c" in args and "copy" in args
    assert str(out) in args


def test_download_writes_bytes(tmp_path):
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"video-bytes"
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None

    with patch("app.stitcher.urlopen", return_value=fake_resp):
        target = tmp_path / "sub" / "x.mp4"
        download("https://x/y.mp4", target)

    assert target.exists()
    assert target.read_bytes() == b"video-bytes"
