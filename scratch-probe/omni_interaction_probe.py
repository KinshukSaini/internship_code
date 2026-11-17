"""Re-GET the Omni interactions a failed run left behind — free, no generation.

Why this exists: a clip that dies with "Omni poll failed ... 400 invalid_request"
tells us nothing about WHICH interaction was rejected or WHY, because the raise
carried neither the interaction id nor a per-beat label (2026-08-05 incident:
clips [0, 3, 9] of a 10-beat run). The ids are already on disk — the pipeline
persists them under manifest["clip_task_ids"] at creation time, before polling —
so the failed run can be interrogated after the fact for free.

It decides between the two live hypotheses:

  H1 permanent per-interaction rejection (safety/validation surfaced on read)
     → the same id 400s again here, hours later. The prompt/anchor for that beat
       is the thing to change.
  H2 read-after-create propagation lag turned fatal by our own policy
     (poll_task_stream_async treats every 4xx as non-retryable, so a 400 on the
     FIRST tick kills a clip that may have been minutes from delivery)
     → the id resolves fine here. The poller's 4xx policy is the thing to change.

Usage:
    .venv/bin/python scratch-probe/omni_interaction_probe.py <run-dir-or-manifest>
    .venv/bin/python scratch-probe/omni_interaction_probe.py --ids id1 id2 id3
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Minimal .env read — this script runs outside the app's Settings."""
    env = ROOT / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _ids_from_manifest(target: Path) -> dict[str, str]:
    manifest = target / "manifest.json" if target.is_dir() else target
    data = json.loads(manifest.read_text())
    ids = data.get("clip_task_ids") or {}
    if not ids:
        # Fall back to the clip records: a run that completed some clips writes
        # task ids there too, and the two disagree exactly when a clip failed.
        ids = {str(c["index"]): c["task_id"]
               for c in (data.get("clips") or []) if c.get("task_id")}
    return {str(k): v for k, v in ids.items()}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    _load_env()
    if argv[0] == "--ids":
        ids = {str(i): v for i, v in enumerate(argv[1:])}
    else:
        ids = _ids_from_manifest(Path(argv[0]))
    if not ids:
        print("no interaction ids found — was this an omni run?")
        return 1

    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    print(f"probing {len(ids)} interaction(s)\n")
    for beat, task_id in sorted(ids.items(), key=lambda kv: int(kv[0])):
        try:
            interaction = client.interactions.get(task_id)
        except Exception as e:  # noqa: BLE001 — every failure mode is a result
            print(f"beat {beat:>3}  {task_id}")
            print(f"          FAILED {type(e).__name__} "
                  f"status={getattr(e, 'status_code', None)}")
            print(f"          body={getattr(e, 'body', None)!r}")
            # The interaction record is often readable WITH its input attached
            # even when the plain read is rejected; when it is, the input is
            # what names the offending argument.
            try:
                verbose = client.interactions.get(task_id, include_input=True)
                print(f"          include_input=True SUCCEEDED → "
                      f"status={getattr(verbose, 'status', None)!r}")
            except Exception as e2:  # noqa: BLE001
                print(f"          include_input=True also failed: "
                      f"{type(e2).__name__}: {str(e2)[:160]}")
            continue
        video = getattr(interaction, "output_video", None)
        print(f"beat {beat:>3}  {task_id}")
        print(f"          status={getattr(interaction, 'status', None)!r} "
              f"artifact={'yes' if getattr(video, 'uri', None) else 'no'}")
        err = getattr(interaction, "error", None)
        if err:
            print(f"          error={err!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
