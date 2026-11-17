"""One-off: add the catalog's shared-library voices to your ElevenLabs account.

Shared-library voices return `voice_not_found` from the TTS API until they're
added to your account. This adds all 15 voices referenced in app.voice's
VOICE_CATALOG so the pipeline can actually use them.

Idempotent: re-running skips voices already in your library. Safe to delete
after a successful run.

    uv run python scripts/add_voices.py            # add them
    uv run python scripts/add_voices.py --dry-run  # just show what would happen
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.config import load_settings
from app.voice import VOICE_CATALOG

BASE = "https://api.elevenlabs.io/v1"


def _catalog_targets() -> dict[str, str]:
    """{voice_id: display_name} for every distinct voice in the catalog."""
    out: dict[str, str] = {}
    for bucket in VOICE_CATALOG.values():
        for entry in bucket.values():
            out.setdefault(entry.voice_id, entry.name)
    return out


def _already_in_account(client: httpx.Client, headers: dict) -> set[str]:
    """voice_ids already present in the account (so we skip them)."""
    r = client.get(f"{BASE}/voices", headers=headers, params={"page_size": 100})
    r.raise_for_status()
    return {v["voice_id"] for v in (r.json().get("voices") or [])}


def _resolve_owners(client: httpx.Client, headers: dict,
                    wanted: set[str]) -> dict[str, dict]:
    """Map each wanted voice_id -> its shared-voices record (has public_owner_id).

    The shared-voices API has no by-id lookup, so we paginate the likely Indian
    filters and match. Stops as soon as all wanted ids are found.
    """
    found: dict[str, dict] = {}

    def scan(params: dict, max_pages: int = 30) -> None:
        page = 0
        while page < max_pages and (wanted - set(found)):
            p = {**params, "page_size": 100, "page": page}
            r = client.get(f"{BASE}/shared-voices", headers=headers, params=p)
            r.raise_for_status()
            data = r.json()
            for v in data.get("voices") or []:
                if v["voice_id"] in wanted and v["voice_id"] not in found:
                    found[v["voice_id"]] = v
            if not data.get("has_more"):
                break
            page += 1

    for params in ({"accent": "indian"}, {"language": "hi"},
                   {"language": "en", "accent": "indian"}):
        if wanted - set(found):
            scan(params)
    return found


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    key = load_settings().elevenlabs_api_key
    if not key:
        print("ELEVENLABS_API_KEY is not set.", file=sys.stderr)
        return 1
    headers = {"xi-api-key": key}

    targets = _catalog_targets()
    print(f"Catalog references {len(targets)} distinct voices.")

    with httpx.Client(timeout=60) as client:
        present = _already_in_account(client, headers)
        todo = {vid: name for vid, name in targets.items() if vid not in present}
        skip = sorted(set(targets) & present)
        if skip:
            print(f"Already in account ({len(skip)}): {', '.join(skip)}")
        if not todo:
            print("Nothing to add — all catalog voices are already in your account.")
            return 0

        print(f"Resolving owner ids for {len(todo)} voice(s) to add...")
        records = _resolve_owners(client, headers, set(todo))
        missing = set(todo) - set(records)
        if missing:
            print(f"WARNING: could not locate in shared library: {sorted(missing)}",
                  file=sys.stderr)

        ok, failed = [], []
        for vid, name in todo.items():
            rec = records.get(vid)
            if not rec:
                failed.append((vid, "not found in shared library"))
                continue
            owner = rec["public_owner_id"]
            add_name = rec.get("name") or name
            if dry_run:
                print(f"  [dry-run] would add {vid}  ({add_name})")
                continue
            try:
                r = client.post(
                    f"{BASE}/voices/add/{owner}/{vid}",
                    headers=headers, json={"new_name": add_name},
                )
                if r.status_code == 200:
                    returned = r.json().get("voice_id", vid)
                    note = "" if returned == vid else f"  (NB: returned id {returned} != catalog id!)"
                    print(f"  added   {vid}  {add_name}{note}")
                    ok.append(vid)
                else:
                    body = r.text[:160]
                    # Treat an 'already added' style error as success.
                    if "already" in body.lower():
                        print(f"  exists  {vid}  {add_name}")
                        ok.append(vid)
                    else:
                        print(f"  FAILED  {vid}  HTTP {r.status_code}: {body}")
                        failed.append((vid, f"HTTP {r.status_code}: {body}"))
            except Exception as e:  # noqa: BLE001
                print(f"  FAILED  {vid}  {type(e).__name__}: {e}")
                failed.append((vid, str(e)))

        if dry_run:
            print("\nDry run complete — no changes made.")
            return 0
        print(f"\nDone. added/ok={len(ok)}, failed={len(failed)}, "
              f"skipped(existing)={len(skip)}.")
        if failed:
            for vid, why in failed:
                print(f"  - {vid}: {why}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
