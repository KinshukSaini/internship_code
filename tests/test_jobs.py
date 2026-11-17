import json
from pathlib import Path

from app.jobs import JobLog


def test_write_initial_creates_file(tmp_path):
    log = JobLog(root=tmp_path)
    log.write_initial(
        task_id="cgt-1",
        prompt="hi",
        assets=[{"url": "u", "kind": "image", "role": "first_frame"}],
        settings={"model": "m", "ratio": "16:9"},
        request_payload={"foo": "bar"},
    )

    f = tmp_path / "cgt-1.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["task_id"] == "cgt-1"
    assert data["prompt"] == "hi"
    assert data["assets"][0]["role"] == "first_frame"
    assert data["status_history"] == []
    assert data["result"] is None


def test_append_status_grows_history(tmp_path):
    log = JobLog(root=tmp_path)
    log.write_initial(task_id="cgt-2", prompt="hi", assets=[],
                     settings={}, request_payload={})
    log.append_status("cgt-2", "queued")
    log.append_status("cgt-2", "running")

    data = json.loads((tmp_path / "cgt-2.json").read_text())
    statuses = [e["status"] for e in data["status_history"]]
    assert statuses == ["queued", "running"]
    assert all("at" in e for e in data["status_history"])


def test_write_final_stores_result(tmp_path):
    log = JobLog(root=tmp_path)
    log.write_initial(task_id="cgt-3", prompt="hi", assets=[],
                     settings={}, request_payload={})
    log.write_final("cgt-3", {"video_url": "https://x/y.mp4", "seed": 1})

    data = json.loads((tmp_path / "cgt-3.json").read_text())
    assert data["result"] == {"video_url": "https://x/y.mp4", "seed": 1}


def test_root_dir_created_on_demand(tmp_path):
    log = JobLog(root=tmp_path / "deep" / "jobs")
    log.write_initial(task_id="cgt-4", prompt="hi", assets=[],
                     settings={}, request_payload={})
    assert (tmp_path / "deep" / "jobs" / "cgt-4.json").exists()
