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


def test_v2_1_fields_round_trip(tmp_path):
    sess = WorkflowSession.new(brief="b", duration_s=15,
                               product_ref_url=None, root=tmp_path)
    sess.start_with_product = True
    sess.use_video_continuity = False
    sess.chunk_local_paths = ["/tmp/a.mp4", "/tmp/b.mp4"]
    sess.stitched_video_path = "/tmp/stitched.mp4"
    save_session(sess)

    loaded = load_session(sess.id, root=tmp_path)
    assert loaded.start_with_product is True
    assert loaded.use_video_continuity is False
    assert loaded.chunk_local_paths == ["/tmp/a.mp4", "/tmp/b.mp4"]
    assert loaded.stitched_video_path == "/tmp/stitched.mp4"


def test_load_session_ignores_unknown_fields(tmp_path):
    """Forward compat: old session JSON with extra/missing fields should
    still load."""
    import json
    p = tmp_path / "wf-old.json"
    p.write_text(json.dumps({
        "id": "wf-old", "state": "draft",
        "brief": "x", "duration_s": 10, "product_ref_url": None,
        "created_at": "2026-01-01T00:00:00",
        "root": str(tmp_path),
        "obsolete_field_from_v0": "ignored",
    }))
    loaded = load_session("wf-old", root=tmp_path)
    assert loaded is not None
    assert loaded.brief == "x"
    # New fields take their defaults
    assert loaded.start_with_product is False
    assert loaded.use_video_continuity is True
