"""Unit tests for resuming a partially-paid run.

The point of resume is money: a stranded run's clips are ALREADY PAID FOR, so
every beat must be resolved by the cheapest tier that works —

    tier 1  a complete local clip-N.mp4          → reuse, FREE
    tier 2  a paid task whose artifact is live    → re-download, FREE
    tier 3  nothing usable                        → re-order, PAID

so these tests assert the tier CHOICE per beat, not just the final render. Every
probe (ffprobe / EvoLink poll / range-GET / download) is faked: a test that
touched the network could both spend credits and go green for the wrong reason.
"""
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.pipeline as P
from app.config import OMNI_VIDEO_MODEL, Settings
from app.pipeline import _Manifest, resumable_runs, resume_plan, resume_ugc_video
from app.voice import Alignment

_SETTINGS = Settings(
    gemini_api_key="x", anthropic_api_key="x", elevenlabs_api_key="x",
    evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
    aws_bucket_name="x", aws_region="x", sync_api_key="x",
)

_WINDOWS = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0)]


def _beat_state(i: int, **ov) -> dict:
    """One persisted effective-beat record (what _beat_state writes)."""
    d = dict(index=i, ad_section="solution", dialogue_text="",
             shot_type="broll", framing=None, broll_type="product_closeup",
             broll_shot="close on the jar", visual_note="the jar sits",
             beat_kind="speech", hold_s=None, shows_creator=False,
             voice_mode="silent", sub_index=0)
    d.update(ov)
    return d


def _motion(i: int, *, provider="seedance", mode="broll") -> dict:
    return {
        "index": i, "mode": mode, "source": "fallback",
        "prompt": f"motion prompt {i}.", "anchor_url": f"https://cdn/a{i}.png",
        "planner_visual_note": "the jar sits", "restaged": False, "model": None,
        "provider": provider,
        "negative_prompt": ("morphing product, warping label"
                            if provider == "kling" else None),
    }


def _write_run(root: Path, run_id: str, *, n=4, task_ids=None, clips=None,
               local=(), extra=None, beats=None, windows=None,
               video_generator="seedance") -> Path:
    """A stranded run on disk: manifest + VO + whichever clips already landed."""
    run = root / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "vo.wav").write_bytes(b"vo")
    for i in local:
        (run / f"clip-{i}.mp4").write_bytes(b"clip")
    windows = _WINDOWS[:n] if windows is None else windows
    align = Alignment(characters=["x"], starts=[0.0],
                      ends=[float(windows[-1][1])])
    data = {
        "version": 1,
        "status": "in_progress",
        "updated_at": "2026-07-24T00:00:00+00:00",
        "brief": "sell the thing",
        "params": {"lipsync_engine": "syncso", "lipsync_granularity": "whole",
                   "clip_resolution": "720p",
                   "broll_video_provider": "seedance",
                   "video_generator": video_generator},
        "out_path": f"jobs/workflows/{run_id}/final.mp4",
        "timeline": {"product_name": "Glow Jar"},
        "beat_windows": [{"start": s, "end": e} for s, e in windows],
        "beats": beats or [_beat_state(i) for i in range(n)],
        "vo_audio_path": str(run / "vo.wav"),
        "vo_duration_s": float(windows[-1][1]),
        "vo_alignment": align.to_dict(),
        "anchors": [{"index": i, "url": f"https://cdn/a{i}.png"}
                    for i in range(n)],
        "motion_prompts": [_motion(i) for i in range(n)],
        "clips": clips or [],
        "clip_task_ids": task_ids or {},
        "character_ref_url": "https://cdn/char.png",
    }
    data.update(extra or {})
    (run / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return run


class _FakeVideoClient:
    """EvoLink stand-in: task ids map to result urls; creates mint new ones."""
    def __init__(self, urls: dict[str, str | None] | None = None):
        self.urls: dict[str, str | None] = dict(urls or {})
        self.created: list = []
        self.polled: list[str] = []
        self.closed = False

    async def create_task_async(self, request):
        task_id = f"new-{len(self.created)}"
        self.created.append(request)
        self.urls[task_id] = f"https://files.evolink.ai/{task_id}.mp4"
        return task_id, {}

    async def poll_task_stream_async(self, task_id, max_wait_seconds=None):
        self.polled.append(task_id)
        url = self.urls.get(task_id)
        if url:
            yield "completed", SimpleNamespace(video_url=url)
        else:
            yield "processing", None

    async def close(self):
        self.closed = True


class _FakeUploader:
    def upload(self, local_path):
        return f"https://cdn/{Path(local_path).name}"


def _install_fakes(monkeypatch, *, client, live=(), assembled=None):
    """Fake every side effect resume can reach. `live` = the urls whose bytes
    still exist (EvoLink retention is ~72h, so most stranded urls are dead)."""
    live = set(live)
    # make_video_client(settings, video_generator) — resume passes the
    # generator the run was rendered with, read back off the manifest.
    monkeypatch.setattr(P, "make_video_client",
                        lambda s, generator="seedance": client)
    monkeypatch.setattr(P, "make_uploader", lambda s: _FakeUploader())
    monkeypatch.setattr(P, "make_lipsync_client", lambda s, e=None: object())
    monkeypatch.setattr(P, "make_voice_client", lambda s, provider=None: object())
    # A file that exists and has bytes probes as a valid clip; anything else is
    # unprobeable (which is exactly how a truncated download must read).
    monkeypatch.setattr(
        P, "_probe_duration",
        lambda p: 1.0 if Path(p).is_file() and Path(p).stat().st_size else 0.0)
    monkeypatch.setattr(P, "_url_is_live", lambda url, **kw: url in live)
    downloads: list[tuple[str, str]] = []

    def _download(url, local):
        downloads.append((url, str(local)))
        Path(local).write_bytes(b"clip")
    monkeypatch.setattr(P, "download", _download)

    assemble_calls: list[dict] = []

    async def _fake_assemble(**kwargs):
        assemble_calls.append(kwargs)
        out = Path(kwargs["out_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"final")
        return SimpleNamespace(path=str(out), duration_s=8.0, n_lipsynced=0)
    monkeypatch.setattr(P, "assemble", assembled or _fake_assemble)
    return downloads, assemble_calls


def _mixed_run(tmp_path: Path) -> Path:
    """A run covering all three tiers at once:

      beat 0  clip on disk AND a task id  → tier 1 (local-first)
      beat 1  task id, artifact still live → tier 2
      beat 2  task id, artifact expired    → tier 3
      beat 3  nothing recorded at all      → tier 3
    """
    return _write_run(
        tmp_path / "workflows", "v2-20260724-101010",
        task_ids={"0": "task-0", "1": "task-1", "2": "task-2"},
        local=(0,),
        extra={"motion_prompts": [_motion(0), _motion(1), _motion(2),
                                  _motion(3, provider="kling")]},
    )


def _write_omni_run(tmp_path: Path, run_id: str, *, n=1, local=(),
                    extra=None) -> Path:
    """An omni run: no VO clock at all — the manifest records
    vo_audio_path=None and no vo_alignment (there is nothing to align; each
    clip authors its own speech), and every beat's motion record is tagged
    provider="omni" so resume routes it through the omni re-order branch.
    """
    ex = {"vo_audio_path": None, "vo_alignment": None}
    ex.update(extra or {})
    return _write_run(
        tmp_path / "workflows", run_id, n=n, local=local,
        video_generator="omni",
        extra={**ex,
               "motion_prompts": [_motion(i, provider="omni")
                                  for i in range(n)]},
    )


@pytest.mark.asyncio
async def test_resume_resolves_every_beat_by_its_cheapest_tier(
    tmp_path, monkeypatch,
):
    """The whole point: local clips are free, a live paid task is free, and only
    a genuine gap costs money."""
    run = _mixed_run(tmp_path)
    client = _FakeVideoClient({"task-0": "https://files.evolink.ai/t0.mp4",
                               "task-1": "https://files.evolink.ai/t1.mp4",
                               "task-2": "https://files.evolink.ai/t2.mp4"})
    downloads, _ = _install_fakes(
        monkeypatch, client=client,
        live=["https://files.evolink.ai/t1.mp4",
              "https://files.evolink.ai/new-0.mp4",
              "https://files.evolink.ai/new-1.mp4"])

    res = await resume_ugc_video(run, settings=_SETTINGS,
                                 visual_qc=False, burn_captions=False,
                                 on_progress=lambda _m: None)

    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["resume"]["tiers"] == {"0": 1, "1": 2, "2": 3, "3": 3}
    # LOCAL-FIRST: beat 0 has a paid task id too, and it must never be polled —
    # the three pre-914536f stranded runs have no task ids at all yet every clip
    # on disk, so a task-id-first resume would re-order work already sitting there.
    assert "task-0" not in client.polled
    assert "task-1" in client.polled
    # Only the two gap beats were re-ordered (2 create_task calls, no more).
    assert len(client.created) == 2
    assert manifest["resume"]["reused_local"] == [0]
    assert manifest["resume"]["redownloaded"] == [1]
    assert manifest["resume"]["reordered"] == [2, 3]
    # The tier-2 artifact was fetched to the canonical name; the local clip was
    # not re-fetched.
    assert (run / "clip-1.mp4").is_file()
    assert ("https://files.evolink.ai/t1.mp4", str(run / "clip-1.mp4")) in downloads
    assert all(dest != str(run / "clip-0.mp4") for _u, dest in downloads)
    assert res.final_video_path.endswith("final.mp4")
    assert client.closed is True


@pytest.mark.asyncio
async def test_resume_reorder_reuses_the_persisted_provider_and_negative(
    tmp_path, monkeypatch,
):
    """A re-ordered clip must be the SAME request the first run would have made
    — including the Kling routing + negative_prompt recorded in stage 5b, and
    the run-dir-derived deterministic seed."""
    from app.config import KLING_BROLL_MODEL
    from app.video_gen import DEFAULT_MODEL_ID

    run = _mixed_run(tmp_path)
    client = _FakeVideoClient({"task-1": "https://files.evolink.ai/t1.mp4"})
    _install_fakes(monkeypatch, client=client,
                   live=["https://files.evolink.ai/t1.mp4",
                         "https://files.evolink.ai/new-0.mp4",
                         "https://files.evolink.ai/new-1.mp4"])

    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)

    by_prompt = {r.prompt: r for r in client.created}
    seedance = by_prompt["motion prompt 2."]
    kling = by_prompt["motion prompt 3."]
    assert seedance.model == DEFAULT_MODEL_ID
    assert seedance.negative_prompt is None
    assert seedance.seed == P._seed_for(run, 2)
    assert kling.model == KLING_BROLL_MODEL
    assert kling.negative_prompt == "morphing product, warping label"
    assert kling.seed is None                  # Kling has no seed at all
    assert kling.duration == 4                 # window-driven, API floor
    assert kling.first_frame_url == "https://cdn/a3.png"
    # Both re-orders are recorded with their provider so a second resume (and
    # the spend estimate) reads the truth back.
    manifest = json.loads((run / "manifest.json").read_text())
    recs = {c["index"]: c for c in manifest["clips"]}
    assert recs[2]["provider"] == "seedance" and recs[3]["provider"] == "kling"
    assert recs[2]["resumed"] and recs[3]["resumed"]
    assert recs[3]["negative_prompt"] == "morphing product, warping label"
    # The tier-2 re-download upserts a record pointing at the canonical local
    # path, so a second resume finds it as tier 1.
    assert recs[1]["local_path"] == str(run / "clip-1.mp4")
    assert recs[1]["redownloaded"] is True


@pytest.mark.asyncio
async def test_resume_omni_reorder_rebuilds_the_native_request(
    tmp_path, monkeypatch,
):
    """An omni re-order is not an EvoLink request at all: no reference audio,
    no negative_prompt, and the RUN-LEVEL seed (index 0) every clip of an omni
    run shares — never the per-beat seed the other providers use."""
    run = _write_omni_run(tmp_path, "v2-20260725-101010")
    client = _FakeVideoClient()
    _install_fakes(monkeypatch, client=client)

    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)

    assert len(client.created) == 1
    req = client.created[0]
    assert req.model == OMNI_VIDEO_MODEL
    assert req.seed == P._seed_for(run, 0)
    assert req.negative_prompt is None
    assert req.audio_url is None
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["clips"][0]["provider"] == "omni"


@pytest.mark.asyncio
async def test_resume_omni_uses_native_lipsync_without_isolated_audio(
    tmp_path, monkeypatch,
):
    """Omni clips arrive with speech already baked in, so resume must route
    them through the same native_lipsync=True assemble() contract as
    seedance_native — and must NOT fall into the isolated-audio fatal check,
    even though the manifest has no beat_audio_paths at all (nothing was ever
    isolated in omni mode)."""
    run = _write_omni_run(tmp_path, "v2-20260725-111111", local=(0,))
    client = _FakeVideoClient()
    _, assemble_calls = _install_fakes(monkeypatch, client=client)

    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)

    assert client.created == []                     # the local clip was reused
    assert assemble_calls[0]["native_lipsync"] is True
    assert assemble_calls[0]["isolated_audio"] is False


@pytest.mark.asyncio
async def test_resume_tier_2_record_carries_the_billing_geometry(
    tmp_path, monkeypatch,
):
    """A tier-2 clip WAS paid for, so its record has to be priceable.

    When the first run died before writing clips[] there is nothing to merge, so
    provider + duration_requested are rebuilt from the motion record — without
    them _clip_cost_estimate prices this already-paid clip at 0s under seedance
    and the per-run spend ledger silently under-reports (a kling clip would even
    be attributed to the wrong provider). An existing record still wins.
    """
    run = _write_run(
        tmp_path / "workflows", "v2-20260724-121212",
        task_ids={"1": "task-1", "2": "task-2"}, local=(0, 3),
        clips=[{"index": 2, "provider": "seedance", "duration_requested": 6,
                "video_url": "https://files.evolink.ai/t2.mp4"}],
        extra={"motion_prompts": [_motion(0), _motion(1, provider="kling"),
                                  _motion(2), _motion(3)]},
    )
    client = _FakeVideoClient({"task-1": "https://files.evolink.ai/t1.mp4",
                               "task-2": "https://files.evolink.ai/t2.mp4"})
    _install_fakes(monkeypatch, client=client,
                   live=["https://files.evolink.ai/t1.mp4",
                         "https://files.evolink.ai/t2.mp4"])

    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)

    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["resume"]["redownloaded"] == [1, 2]
    recs = {c["index"]: c for c in manifest["clips"]}
    # Beat 1 had NO prior record: geometry rebuilt from motion_prompts[1].
    assert recs[1]["provider"] == "kling"
    assert recs[1]["duration_requested"] == 4      # 2s window → API floor
    # Beat 2 HAD one: its own recorded 6s ordered duration survives the merge.
    assert recs[2]["duration_requested"] == 6
    assert recs[2]["video_url"] == "https://files.evolink.ai/t2.mp4"
    # Nothing was re-ordered, so the resume's own spend really is $0 — the
    # ledger fix must not turn a free tier into a billed one.
    assert manifest["resume"]["reordered"] == []
    assert manifest["resume"]["paid_cost"]["total_credits"] == 0
    # ...and the run-level ledger can now price every clip it holds.
    full = P._clip_cost_estimate(recs)
    assert full["by_provider"]["kling"]["seconds"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_resume_logs_only_the_reordered_spend(tmp_path, monkeypatch):
    """Cost visibility: the run is at ~$100 of credits, so a resume must report
    what it actually spends — the free tiers are NOT billed again."""
    run = _mixed_run(tmp_path)
    client = _FakeVideoClient({"task-1": "https://files.evolink.ai/t1.mp4"})
    _install_fakes(monkeypatch, client=client,
                   live=["https://files.evolink.ai/t1.mp4",
                         "https://files.evolink.ai/new-0.mp4",
                         "https://files.evolink.ai/new-1.mp4"])
    logs: list[str] = []
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=logs.append)

    cost = json.loads((run / "manifest.json").read_text())["resume"]["paid_cost"]
    # Beat 2 on seedance (4s × 13.5) + beat 3 on kling (4s × 5.4).
    assert cost["by_provider"]["seedance"]["credits"] == pytest.approx(54.0)
    assert cost["by_provider"]["kling"]["credits"] == pytest.approx(21.6)
    assert cost["total_credits"] == pytest.approx(75.6)
    assert any("resume spend" in m for m in logs)
    # Every beat's tier is logged, so a surprise re-order is visible in the log.
    assert sum(1 for m in logs if "local clip (free)" in m) == 1
    assert sum(1 for m in logs if "re-downloaded" in m) == 1


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_free_the_second_time(
    tmp_path, monkeypatch,
):
    """Side-effect-safe to call twice: after the first pass every clip is on
    disk, so the second resume is all tier 1 and spends nothing."""
    run = _mixed_run(tmp_path)
    client = _FakeVideoClient({"task-1": "https://files.evolink.ai/t1.mp4"})
    _install_fakes(monkeypatch, client=client,
                   live=["https://files.evolink.ai/t1.mp4",
                         "https://files.evolink.ai/new-0.mp4",
                         "https://files.evolink.ai/new-1.mp4"])
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)
    n_created = len(client.created)

    client2 = _FakeVideoClient()
    _install_fakes(monkeypatch, client=client2)      # nothing live, no urls
    logs: list[str] = []
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=logs.append)

    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["resume"]["tiers"] == {"0": 1, "1": 1, "2": 1, "3": 1}
    assert client2.created == []                    # zero paid calls
    assert client2.polled == []                     # local-first: no polling
    assert n_created == 2
    assert any("$0" in m for m in logs)


@pytest.mark.asyncio
async def test_resume_refuses_to_spend_when_paid_reorder_is_disallowed(
    tmp_path, monkeypatch,
):
    """The "$0 only" path: refuse BEFORE any create_task, never half-spend."""
    run = _mixed_run(tmp_path)
    client = _FakeVideoClient({"task-1": "https://files.evolink.ai/t1.mp4"})
    _install_fakes(monkeypatch, client=client,
                   live=["https://files.evolink.ai/t1.mp4"])
    with pytest.raises(RuntimeError, match="allow_paid_reorder=False"):
        await resume_ugc_video(run, settings=_SETTINGS,
                               allow_paid_reorder=False, visual_qc=False,
                               burn_captions=False, on_progress=lambda _m: None)
    assert client.created == []
    assert client.closed is True                    # client still torn down


@pytest.mark.asyncio
async def test_resume_plan_reports_the_tiers_without_spending(
    tmp_path, monkeypatch,
):
    """The UI's pre-commit breakdown: polls + range-GETs (both free) but
    downloads nothing and orders nothing."""
    run = _mixed_run(tmp_path)
    client = _FakeVideoClient({"task-1": "https://files.evolink.ai/t1.mp4",
                               "task-2": "https://files.evolink.ai/t2.mp4"})
    downloads, _ = _install_fakes(
        monkeypatch, client=client, live=["https://files.evolink.ai/t1.mp4"])

    plan = await resume_plan(run, settings=_SETTINGS,
                            on_progress=lambda _m: None)

    assert plan["tiers"] == {0: 1, 1: 2, 2: 3, 3: 3}
    assert plan["free"] == [0, 1] and plan["paid"] == [2, 3]
    assert plan["paid_usd"] == pytest.approx(75.6 * 0.014722)
    assert plan["unrecoverable"] == []
    assert client.created == []
    assert downloads == []
    assert not (run / "clip-1.mp4").is_file()   # tier 2 predicted, not fetched
    assert "beat 2: re-ordered on EvoLink (PAID)" in plan["summary"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_resume_plan_flags_a_gap_with_no_anchor_to_reorder_from(
    tmp_path, monkeypatch,
):
    """A tier-3 beat with no anchor image / no motion prompt cannot be
    re-ordered at all — that has to surface BEFORE the user commits."""
    run = _write_run(tmp_path / "workflows", "v2-20260724-111111", n=2,
                     extra={"anchors": [{"index": 0, "url": "https://cdn/a0.png"}],
                            "motion_prompts": [_motion(0)]})
    client = _FakeVideoClient()
    _install_fakes(monkeypatch, client=client)
    plan = await resume_plan(run, settings=_SETTINGS,
                            on_progress=lambda _m: None)
    assert plan["unrecoverable"] == [1]
    assert "beats [1]" in plan["summary"]


@pytest.mark.asyncio
async def test_resume_falls_to_tier_3_when_a_download_lands_unplayable(
    tmp_path, monkeypatch,
):
    """A killed download leaves a truncated mp4 behind and reusing one poisons
    the concat — an unprobeable re-download must fall through to a re-order."""
    run = _write_run(tmp_path / "workflows", "v2-20260724-121212", n=1,
                     task_ids={"0": "task-0"})
    client = _FakeVideoClient({"task-0": "https://files.evolink.ai/t0.mp4"})
    _install_fakes(monkeypatch, client=client,
                   live=["https://files.evolink.ai/t0.mp4",
                         "https://files.evolink.ai/new-0.mp4"])
    # The tier-2 fetch writes a zero-byte file; the re-order writes a real one.
    real_download = P.download

    def _flaky(url, local):
        if url == "https://files.evolink.ai/t0.mp4":
            Path(local).write_bytes(b"")
            return
        real_download(url, local)
    monkeypatch.setattr(P, "download", _flaky)

    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["resume"]["tiers"] == {"0": 3}
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_resume_ignores_a_completed_task_whose_artifact_expired(
    tmp_path, monkeypatch,
):
    """EvoLink keeps the task row (status "completed", result url present) long
    after the file 404s (~72h retention, probed by age: 0/1/2d → 206, 4d+ →
    404). Liveness is decided by BYTES, never by the status field."""
    run = _write_run(tmp_path / "workflows", "v2-20260724-131313", n=1,
                     task_ids={"0": "task-0"})
    client = _FakeVideoClient({"task-0": "https://files.evolink.ai/t0.mp4"})
    downloads, _ = _install_fakes(
        monkeypatch, client=client,
        live=["https://files.evolink.ai/new-0.mp4"])   # the old url is dead

    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["resume"]["tiers"] == {"0": 3}
    assert "task-0" in client.polled                  # the record WAS trusted…
    assert all(u != "https://files.evolink.ai/t0.mp4" for u, _d in downloads)


@pytest.mark.asyncio
async def test_resume_probes_the_recorded_url_when_no_task_id_exists(
    tmp_path, monkeypatch,
):
    """Pre-914536f runs recorded no task ids but DID record each landed clip's
    result url — still worth a liveness probe before paying again."""
    run = _write_run(
        tmp_path / "workflows", "v2-20260724-141414", n=1,
        clips=[{"index": 0, "evolink_url": "https://files.evolink.ai/old.mp4",
                "local_path": "jobs/workflows/gone/clip-0.mp4",
                "duration_requested": 4, "provider": "seedance"}],
    )
    client = _FakeVideoClient()
    downloads, _ = _install_fakes(
        monkeypatch, client=client, live=["https://files.evolink.ai/old.mp4"])
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["resume"]["tiers"] == {"0": 2}
    assert client.created == []
    assert downloads == [("https://files.evolink.ai/old.mp4",
                          str(run / "clip-0.mp4"))]


@pytest.mark.asyncio
async def test_resume_reuses_already_paid_lipsync_records(tmp_path, monkeypatch):
    """THE BUG FIX (pipeline.py:1490): the assemble() call site used to guard
    with `hasattr(manifest, "get")` and _Manifest had no `get`, so
    prior_lipsync_records was PERMANENTLY None and every talking head was
    re-lipsynced — re-paid — on every retry."""
    prior = [{"index": 0, "output_url": "https://cdn/lip-0.mp4"}]
    run = _write_run(tmp_path / "workflows", "v2-20260724-151515", n=1,
                     local=(0,), extra={"lipsync": prior})
    client = _FakeVideoClient()
    _, assemble_calls = _install_fakes(monkeypatch, client=client)
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)
    assert assemble_calls[0]["prior_lipsync_records"] == prior


@pytest.mark.asyncio
async def test_resume_passes_the_rehydrated_geometry_into_assembly(
    tmp_path, monkeypatch,
):
    """Resume must finish through the SAME tail as a fresh run, fed the
    EFFECTIVE (post-split/merge) beats and windows the clips were sized to —
    real TimelineBeat instances, not dicts."""
    from app.gemini import TimelineBeat
    run = _mixed_run(tmp_path)
    client = _FakeVideoClient({"task-1": "https://files.evolink.ai/t1.mp4"})
    _, assemble_calls = _install_fakes(
        monkeypatch, client=client,
        live=["https://files.evolink.ai/t1.mp4",
              "https://files.evolink.ai/new-0.mp4",
              "https://files.evolink.ai/new-1.mp4"])
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=False, on_progress=lambda _m: None)
    kwargs = assemble_calls[0]
    assert kwargs["windows"] == _WINDOWS
    assert all(isinstance(b, TimelineBeat) for b in kwargs["beats"])
    # clip_paths are index-ordered — a dict-ordering slip here would silently
    # scramble the cut.
    assert kwargs["clip_paths"] == [str(run / f"clip-{i}.mp4")
                                    for i in range(4)]
    assert kwargs["emphasis_terms"] == ("Glow", "Jar")   # planner product_name


@pytest.mark.asyncio
async def test_resume_refuses_a_manifest_that_predates_the_beats_record(
    tmp_path, monkeypatch,
):
    """v2-20260709-191535 has 10 planner beats for 9 merged windows: the
    effective beat list cannot be rebuilt, and guessing would desync the cut.
    Refuse BEFORE a client is built, i.e. before anything can be spent."""
    run = _write_run(tmp_path / "workflows", "v2-20260724-161616", n=3,
                     beats=[_beat_state(i) for i in range(4)])
    client = _FakeVideoClient()
    _install_fakes(monkeypatch, client=client)
    with pytest.raises(ValueError, match="4 beats for 3 windows"):
        await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                               on_progress=lambda _m: None)
    assert client.created == []


@pytest.mark.asyncio
async def test_resume_disables_captions_on_a_legacy_synthetic_alignment(
    tmp_path, monkeypatch,
):
    """Pre-2026-07-25 manifests kept only the VO duration. Word-popping a
    synthetic one-character alignment would burn garbage over the master, so
    captions are force-disabled (with a logged reason) instead."""
    run = _write_run(tmp_path / "workflows", "v2-20260724-171717", n=1,
                     local=(0,))
    data = json.loads((run / "manifest.json").read_text())
    del data["vo_alignment"]
    (run / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    client = _FakeVideoClient()
    _, assemble_calls = _install_fakes(monkeypatch, client=client)
    logs: list[str] = []
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=True, on_progress=logs.append)
    assert assemble_calls[0]["burn_captions"] is False
    assert any("captions disabled" in m for m in logs)


@pytest.mark.asyncio
async def test_resume_keeps_captions_enabled_on_an_omni_synthetic_alignment(
    tmp_path, monkeypatch,
):
    """Omni manifests NEVER carry a vo_alignment by design (there is no VO to
    align — every clip authors its own speech), so the synthetic alignment
    here is not a legacy-manifest defect. Omni recovers caption timing by
    force-aligning the known script against the finished video, same as
    seedance_native, so it belongs on that engine's exemption list rather
    than tripping the "captions disabled" guard."""
    run = _write_omni_run(tmp_path, "v2-20260725-121212", local=(0,))
    client = _FakeVideoClient()
    _, assemble_calls = _install_fakes(monkeypatch, client=client)
    logs: list[str] = []
    await resume_ugc_video(run, settings=_SETTINGS, visual_qc=False,
                           burn_captions=True, on_progress=logs.append)
    assert assemble_calls[0]["burn_captions"] is True
    assert not any("captions disabled" in m for m in logs)


@pytest.mark.asyncio
async def test_resume_of_an_omni_run_needs_no_elevenlabs_key(
    tmp_path, monkeypatch,
):
    """generate_ugc_video legally produces (and bills) an omni run with a blank
    ELEVENLABS_API_KEY — captions just skip. So resume must not demand one:
    building the client unconditionally raised before a single tier was resolved
    and made every already-paid clip unrecoverable through this entry point.
    The real factory is restored here — that RuntimeError lives in its ctor."""
    from app.voice import make_voice_client as real_make_voice_client
    run = _write_omni_run(tmp_path, "v2-20260725-131313", local=(0,))
    client = _FakeVideoClient()
    _, assemble_calls = _install_fakes(monkeypatch, client=client)
    monkeypatch.setattr(P, "make_voice_client", real_make_voice_client)
    keyless = dataclasses.replace(_SETTINGS, elevenlabs_api_key="")

    res = await resume_ugc_video(run, settings=keyless, visual_qc=False,
                                 burn_captions=True, on_progress=lambda _m: None)

    assert res.final_video_path.endswith("final.mp4")
    # No force-aligner exists without the key → captions degrade, run survives.
    assert assemble_calls[0]["force_align"] is None


@pytest.mark.asyncio
async def test_resume_plan_probes_an_omni_task_without_fetching_the_clip(
    tmp_path, monkeypatch,
):
    """The free preview must stay free AND side-effect-free. An omni clip is
    materialized by its POLLER (download from the Files API + a fresh R2 write),
    so the plan has to use the client's status-only probe instead — otherwise
    every Preview click pulls each completed clip and mints a duplicate object.
    """
    run = _write_omni_run(tmp_path, "v2-20260725-141414", n=1,
                          extra={"clip_task_ids": {"0": "task-0"}})

    class _SpyOmniClient(_FakeVideoClient):
        def __init__(self):
            super().__init__()
            self.probed: list[str] = []

        async def probe_status_async(self, task_id):
            self.probed.append(task_id)
            return "completed", True

    client = _SpyOmniClient()
    downloads, _ = _install_fakes(monkeypatch, client=client)

    plan = await resume_plan(run, settings=_SETTINGS,
                            on_progress=lambda _m: None)

    assert plan["tiers"] == {0: 2}          # recoverable for free
    assert plan["paid"] == [] and plan["free"] == [0]
    assert client.probed == ["task-0"]
    assert client.polled == []              # the delivering poller never ran
    assert downloads == []


# --- resumable_runs (feeds the UI dropdown; zero network) -------------------

def test_resumable_runs_lists_paid_runs_newest_first(tmp_path, monkeypatch):
    root = tmp_path / "workflows"
    monkeypatch.setattr(
        P, "_probe_duration",
        lambda p: 1.0 if Path(p).is_file() and Path(p).stat().st_size else 0.0)
    _write_run(root, "v2-20260701-090000", n=3, local=(0, 1),
               task_ids={"2": "task-2"})
    _write_run(root, "v2-20260702-090000", n=2, local=(0,))
    runs = resumable_runs(root)
    assert [r.run_id for r in runs] == ["v2-20260702-090000",
                                        "v2-20260701-090000"]
    older = runs[1]
    assert older.local == [0, 1] and older.paid_task == [2]
    assert older.missing == []
    assert older.resumable is True
    assert older.label == "v2-20260701-090000  (3/3 clips paid)"
    assert older.to_dict()["resumable"] is True


def test_resumable_runs_skips_finished_runs(tmp_path, monkeypatch):
    root = tmp_path / "workflows"
    monkeypatch.setattr(
        P, "_probe_duration",
        lambda p: 1.0 if Path(p).is_file() and Path(p).stat().st_size else 0.0)
    run = _write_run(root, "v2-20260703-090000", n=1, local=(0,))
    (run / "final.mp4").write_bytes(b"final")
    assert resumable_runs(root) == []


def test_resumable_runs_shows_blocked_runs_instead_of_hiding_them(
    tmp_path, monkeypatch,
):
    """A run that can't be resumed still holds PAID clips — silently hiding it
    is worse than showing why it is blocked."""
    root = tmp_path / "workflows"
    monkeypatch.setattr(
        P, "_probe_duration",
        lambda p: 1.0 if Path(p).is_file() and Path(p).stat().st_size else 0.0)
    _write_run(root, "v2-20260704-090000", n=2, local=(0,),
               beats=[_beat_state(i) for i in range(3)])
    _write_run(root, "v2-20260705-090000", n=1, local=(0,),
               extra={"beat_windows": []})
    (root / "corrupt").mkdir(parents=True)
    (root / "corrupt" / "manifest.json").write_text("{not json", encoding="utf-8")
    runs = {r.run_id: r for r in resumable_runs(root)}
    assert "corrupt" not in runs                 # unparseable → not listed
    mismatch = runs["v2-20260704-090000"]
    assert mismatch.resumable is False
    assert "predates the beats record" in mismatch.blocked_reason
    assert "[" in mismatch.label                 # the reason rides the label
    no_windows = runs["v2-20260705-090000"]
    assert no_windows.resumable is False
    assert "beat windows" in no_windows.blocked_reason


def test_resumable_runs_on_a_missing_root_is_empty(tmp_path):
    assert resumable_runs(tmp_path / "nope") == []


def test_the_ui_writes_runs_where_resume_looks_for_them():
    """The two halves of the feature must agree on ONE tree.

    resumable_runs() defaults to the ROOT-anchored WORKFLOWS_ROOT while the UI
    used to build run dirs from the cwd-relative literal Path("jobs/workflows").
    Identical when the app is launched from the repo root (which is why the
    dropdown found the real stranded runs), but launched from anywhere else a
    new run's PAID clips become invisible to resume — unsalvageable. Asserted on
    the source because the run dir is built inside a Gradio streaming generator
    that a unit test cannot reach.
    """
    import inspect

    import app.ui as ui
    src = inspect.getsource(ui)
    assert 'Path("jobs/workflows")' not in src
    assert "WORKFLOWS_ROOT / f\"v2-{ts}\"" in src
    assert ui.WORKFLOWS_ROOT is P.WORKFLOWS_ROOT


# --- _Manifest.get / .load (the two halves of the write-only-state bug) ----

def test_manifest_get_returns_written_values(tmp_path):
    man = _Manifest(tmp_path / "manifest.json")
    man.update(lipsync=[{"index": 0}], clip_task_ids={"0": "t0"})
    assert man.get("lipsync") == [{"index": 0}]
    assert man.get("clip_task_ids") == {"0": "t0"}
    assert man.get("nope") is None
    assert man.get("nope", "fallback") == "fallback"


def test_manifest_load_round_trips_a_manifest_from_disk(tmp_path):
    """__init__ always built a fresh _data dict and nothing ever read the file
    back, so every "resumable record" the pipeline wrote was write-only."""
    path = tmp_path / "manifest.json"
    original = _Manifest(path)
    original.update(brief="sell the thing", clips=[{"index": 0}],
                    clip_task_ids={"0": "task-0"})
    original.add_step("clip", index=0, prompt="p", produced={"ok": True})

    loaded = _Manifest.load(path)
    assert loaded.get("brief") == "sell the thing"
    assert loaded.get("clips") == [{"index": 0}]
    assert loaded.get("clip_task_ids") == {"0": "task-0"}
    assert loaded.get("version") == 1            # never rewrite what was paid
    assert [s["step"] for s in loaded.get("steps")] == ["clip"]
    # Still a live manifest: an update on the rehydrated object persists and
    # keeps the earlier state.
    loaded.update(resume={"tiers": {"0": 1}})
    again = _Manifest.load(path)
    assert again.get("resume") == {"tiers": {"0": 1}}
    assert again.get("brief") == "sell the thing"


def test_manifest_load_raises_a_clear_error_on_missing_or_garbage(tmp_path):
    with pytest.raises(FileNotFoundError, match="nothing to resume"):
        _Manifest.load(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        _Manifest.load(bad)
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        _Manifest.load(lst)


# --- the liveness probe itself ---------------------------------------------

def test_url_is_live_range_gets_one_byte_with_the_browser_ua(monkeypatch):
    """files.evolink.ai 403s the default Python-urllib agent, which would read
    as "dead" for every still-live clip — so the probe must send the browser UA
    and ask for a single byte, not the whole file."""
    import contextlib
    seen: list = []

    @contextlib.contextmanager
    def _fake_urlopen(req, timeout=None):
        seen.append(req)
        yield SimpleNamespace(status=206)
    monkeypatch.setattr(P, "urlopen", _fake_urlopen)

    assert P._url_is_live("https://files.evolink.ai/x.mp4") is True
    req = seen[0]
    assert req.get_header("Range") == "bytes=0-0"
    assert "Mozilla" in (req.get_header("User-agent") or "")


def test_url_is_live_is_false_on_an_expired_artifact(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def _boom(req, timeout=None):
        raise OSError("HTTP 404")
        yield  # pragma: no cover
    monkeypatch.setattr(P, "urlopen", _boom)
    assert P._url_is_live("https://files.evolink.ai/gone.mp4") is False


def test_clip_is_usable_rejects_empty_and_absent_files(tmp_path, monkeypatch):
    """Presence is not enough: a killed download leaves a truncated file that
    silently poisons the concat."""
    monkeypatch.setattr(P, "_probe_duration", lambda p: 1.0)
    assert P._clip_is_usable(tmp_path / "absent.mp4") is False
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert P._clip_is_usable(empty) is False
    good = tmp_path / "good.mp4"
    good.write_bytes(b"clip")
    assert P._clip_is_usable(good) is True
    # An unprobeable file (ffprobe raises) is an unusable file.
    monkeypatch.setattr(P, "_probe_duration",
                        lambda p: (_ for _ in ()).throw(RuntimeError("bad mp4")))
    assert P._clip_is_usable(good) is False
