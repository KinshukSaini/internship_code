from pathlib import Path
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from app.config import Settings
from app.uploader import S3Uploader, content_hash, make_uploader, s3_key_for


def _settings(**overrides):
    base = dict(
        evolink_api_key="evo",
        aws_access_key_id="akid",
        aws_secret_access_key="secret",
        aws_bucket_name="my-bucket",
        aws_region="auto",
        gemini_api_key="g",
        elevenlabs_api_key="e",
        sync_api_key="s",
    )
    base.update(overrides)
    return Settings(**base)


def test_content_hash_stable(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello world")
    assert content_hash(p) == content_hash(p)
    assert len(content_hash(p)) == 64


def test_content_hash_changes_with_content(tmp_path):
    a = tmp_path / "a.bin"; a.write_bytes(b"aaa")
    b = tmp_path / "b.bin"; b.write_bytes(b"bbb")
    assert content_hash(a) != content_hash(b)


def test_s3_key_preserves_extension():
    key = s3_key_for("abcdef1234", "/some/path/picture.JPG")
    assert key == "product_flatlays/crops/abcdef1234.jpg"


def test_s3_key_handles_no_extension():
    key = s3_key_for("abc", "/some/path/picture")
    assert key == "product_flatlays/crops/abc"


def test_upload_skips_when_object_exists(tmp_path):
    p = tmp_path / "f.png"; p.write_bytes(b"data")
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 4}

    u = S3Uploader(s3, bucket="my-bucket", region="ap-south-1")
    url = u.upload(str(p))

    s3.head_object.assert_called_once()
    s3.upload_file.assert_not_called()
    assert url.startswith("https://my-bucket.s3.ap-south-1.amazonaws.com/")
    assert url.endswith(".png")


def test_upload_puts_when_object_missing(tmp_path):
    p = tmp_path / "f.png"; p.write_bytes(b"data")
    s3 = MagicMock()
    err = botocore.exceptions.ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    s3.head_object.side_effect = err

    u = S3Uploader(s3, bucket="my-bucket", region="ap-south-1")
    url = u.upload(str(p))

    s3.upload_file.assert_called_once()
    assert "my-bucket" in url


def test_public_base_url_overrides_aws_host(tmp_path):
    """R2 / custom stores: URL is {public_base}/{key}, not the AWS host."""
    p = tmp_path / "f.png"; p.write_bytes(b"data")
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 4}

    u = S3Uploader(
        s3, bucket="my-bucket", region="auto",
        public_base_url="https://pub-abc.r2.dev/",  # trailing slash trimmed
    )
    url = u.upload(str(p))

    assert url.startswith("https://pub-abc.r2.dev/product_flatlays/crops/")
    assert "amazonaws.com" not in url
    assert url.endswith(".png")


def test_make_uploader_wires_r2_endpoint_and_checksum_config():
    settings = _settings(
        s3_endpoint_url="https://acct.r2.cloudflarestorage.com",
        s3_public_base_url="https://pub-abc.r2.dev",
    )
    captured = {}

    def fake_client(service, **kwargs):
        captured.update(service=service, **kwargs)
        return MagicMock()

    with patch("boto3.client", fake_client):
        up = make_uploader(settings)

    assert captured["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"
    cfg = captured["config"]
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert up._public_base_url == "https://pub-abc.r2.dev"


def test_make_uploader_omits_endpoint_and_config_for_plain_aws():
    settings = _settings()  # no S3 endpoint set
    captured = {}

    def fake_client(service, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch("boto3.client", fake_client):
        up = make_uploader(settings)

    assert "endpoint_url" not in captured
    assert "config" not in captured
    assert up._public_base_url == ""


def test_upload_raises_on_unexpected_head_error(tmp_path):
    p = tmp_path / "f.png"; p.write_bytes(b"data")
    s3 = MagicMock()
    err = botocore.exceptions.ClientError(
        {"Error": {"Code": "500", "Message": "boom"}}, "HeadObject"
    )
    s3.head_object.side_effect = err

    u = S3Uploader(s3, bucket="my-bucket", region="ap-south-1")
    with pytest.raises(botocore.exceptions.ClientError):
        u.upload(str(p))


def test_upload_treats_403_as_missing(tmp_path):
    """Buckets that hide existence (no s3:ListBucket) return 403 for HEAD;
    we should fall through to PUT rather than abort."""
    p = tmp_path / "f.png"; p.write_bytes(b"data")
    s3 = MagicMock()
    err = botocore.exceptions.ClientError(
        {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
    )
    s3.head_object.side_effect = err

    u = S3Uploader(s3, bucket="my-bucket", region="ap-south-1")
    url = u.upload(str(p))

    s3.upload_file.assert_called_once()
    assert "my-bucket" in url
