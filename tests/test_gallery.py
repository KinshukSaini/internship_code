import json
from pathlib import Path
from app.gallery import Gallery
from app.image_gen import GeneratedImage


def _img(url="https://x/a.jpeg", source="tab3"):
    return GeneratedImage.from_url(
        url=url, prompt="p", size="2K", source=source,
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
