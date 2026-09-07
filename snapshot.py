#!/usr/bin/env python3
"""Capture and restore AniList + Simkl state. The safety net for a live run.

Dry run proves what *this service intends*. It cannot prove what the other end
does with it - the 2026-09-07 incident happened because Simkl read a payload as
"mark the whole show watched" and the dry run had no way to know. So before any
live run, take a snapshot; if something goes wrong, diff it and restore.

    python snapshot.py save                  # writes snapshots/<timestamp>.json
    python snapshot.py diff  <file>          # what changed since that snapshot
    python snapshot.py restore <file>        # dry run of the corrective writes
    python snapshot.py restore <file> --commit
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aniprogress.anilist import AniList  # noqa: E402
from aniprogress.config import Config, load_dotenv  # noqa: E402
from aniprogress.simkl import Simkl  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("snapshot")

SNAP_DIR = os.environ.get("SNAPSHOT_DIR", "snapshots")
EPOCH = "2010-01-01T00:00:00Z"


def _clients(dry_run: bool):
    load_dotenv(".env")
    cfg = Config()
    return (
        AniList(cfg.anilist_token, dry_run=dry_run),
        Simkl(cfg.simkl_client_id, cfg.simkl_token, dry_run=dry_run),
    )


def _read() -> dict:
    anilist, simkl = _clients(dry_run=True)
    al = {}
    for e in anilist.list_entries():
        m = (e.get("media") or {}).get("idMal")
        if not m:
            continue
        raw = e.get("scoreRaw")
        al[str(int(m))] = {
            "anilist_id": (e.get("media") or {}).get("id"),
            "title": ((e.get("media") or {}).get("title") or {}).get("romaji"),
            "status": e.get("status"),
            "progress": int(e.get("progress") or 0),
            "score": round(int(raw) / 10.0, 1) if raw else None,
        }
    sk = {}
    for r in Simkl.anime_entries(simkl.all_items("anime", date_from=EPOCH)):
        ids = (r.get("show") or {}).get("ids") or {}
        m = ids.get("mal")
        if not m:
            continue
        sk[str(int(m))] = {
            "title": (r.get("show") or {}).get("title"),
            "status": r.get("status"),
            "watched": r.get("watched_episodes_count"),
            "rating": r.get("user_rating"),
        }
    return {"taken_at": dt.datetime.now(dt.timezone.utc).isoformat(), "anilist": al, "simkl": sk}


def cmd_save() -> int:
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = _read()
    name = os.path.join(SNAP_DIR, dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    json.dump(snap, open(name, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    log.info("saved %s - anilist %d, simkl %d", name, len(snap["anilist"]), len(snap["simkl"]))
    return 0


def _diff(old: dict, new: dict) -> tuple[list, list]:
    al_changes, sk_changes = [], []
    for mal, was in old["anilist"].items():
        now = new["anilist"].get(mal)
        if now and (was["status"] != now["status"] or was["progress"] != now["progress"]):
            al_changes.append((mal, was, now))
    for mal, was in old["simkl"].items():
        now = new["simkl"].get(mal)
        if now is None:
            sk_changes.append((mal, was, None))
        elif was["watched"] != now["watched"] or was["status"] != now["status"]:
            sk_changes.append((mal, was, now))
    for mal, now in new["simkl"].items():
        if mal not in old["simkl"]:
            sk_changes.append((mal, None, now))
    return al_changes, sk_changes


def cmd_diff(path: str) -> int:
    old = json.load(open(path, encoding="utf-8"))
    new = _read()
    al_changes, sk_changes = _diff(old, new)
    log.info("since %s", old["taken_at"])
    log.info("ANILIST changed: %d", len(al_changes))
    for mal, was, now in al_changes:
        log.info("  %-40.40s %s/ep%s -> %s/ep%s", was["title"],
                 was["status"], was["progress"], now["status"], now["progress"])
    log.info("SIMKL changed: %d", len(sk_changes))
    for mal, was, now in sk_changes:
        if was is None:
            log.info("  %-40.40s ADDED (watched %s)", now["title"], now["watched"])
        elif now is None:
            log.info("  %-40.40s REMOVED (was watched %s)", was["title"], was["watched"])
        else:
            bits = []
            if was["watched"] != now["watched"]:
                bits.append(f"watched {was['watched']} -> {now['watched']}")
            if was["status"] != now["status"]:
                bits.append(f"status {was['status']} -> {now['status']}")
            log.info("  %-40.40s %s", was["title"], ", ".join(bits))
    return 0


def cmd_restore(path: str, commit: bool) -> int:
    old = json.load(open(path, encoding="utf-8"))
    new = _read()
    al_changes, sk_changes = _diff(old, new)
    anilist, simkl = _clients(dry_run=not commit)

    log.info("ANILIST - put %d entr(ies) back", len(al_changes))
    for mal, was, now in al_changes:
        log.info("  %-40.40s back to %s/ep%s", was["title"], was["status"], was["progress"])
        if commit and was.get("anilist_id"):
            anilist.save(int(was["anilist_id"]), status=was["status"],
                         progress=int(was["progress"]))

    # Only additions are undone. Removing history we did not add, or re-adding
    # history that was legitimately removed, would be a second incident.
    added = [c for c in sk_changes if c[1] is None]
    grew = [c for c in sk_changes if c[1] and c[2] and (c[2]["watched"] or 0) > (c[1]["watched"] or 0)]
    log.info("SIMKL - remove %d added title(s), reset %d that grew",
             len(added), len(grew))
    for mal, was, now in added:
        log.info("  %-40.40s remove all history", now["title"])
        if commit:
            simkl.remove_history({"anime": [{"ids": {"mal": int(mal)}}]})
    for mal, was, now in grew:
        keep = int(was["watched"] or 0)
        log.info("  %-40.40s watched %s -> back to %s", was["title"], now["watched"], keep)
        if commit:
            drop = [{"number": n} for n in range(keep + 1, int(now["watched"] or 0) + 1)]
            simkl.remove_history({"anime": [{"ids": {"mal": int(mal)}, "episodes": drop}]})

    log.info("DONE" if commit else "DRY RUN - re-run with --commit to apply")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "save":
        return cmd_save()
    if cmd == "diff" and len(sys.argv) > 2:
        return cmd_diff(sys.argv[2])
    if cmd == "restore" and len(sys.argv) > 2:
        return cmd_restore(sys.argv[2], "--commit" in sys.argv)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
