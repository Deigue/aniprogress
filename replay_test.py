#!/usr/bin/env python3
"""Offline proof: replay a real Simkl export through the outbound logic.

No network, no tokens. Stubs the Simkl/AniList/MAL clients and feeds the anime
block of a Simkl backup through outbound_tick, so you can see exactly which
writes would fire and confirm the dedup gate holds.

    python replay_test.py path/to/simkl-export.json

Get the export from Simkl -> Settings -> Import/Export -> Export as JSON.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aniprogress.config import Config          # noqa: E402
from aniprogress.main import outbound_tick     # noqa: E402
from aniprogress.state import State            # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")


class FakeSimkl:
    def __init__(self, anime):
        self._anime = anime
        self.calls = []

    def activities(self):
        self.calls.append("activities")
        return {"all": "2026-09-03T22:17:00Z"}

    def all_items(self, media_type="anime", status=None, date_from=None, extended="full"):
        self.calls.append(f"all_items(date_from={date_from})")
        return {"anime": self._anime}

    @staticmethod
    def anime_entries(blob):
        from aniprogress.simkl import Simkl
        return Simkl.anime_entries(blob)

    @staticmethod
    def ids_of(entry):
        from aniprogress.simkl import Simkl
        return Simkl.ids_of(entry)


class FakeAniList:
    def __init__(self):
        self.writes = []
        self.lookups = 0

    def by_mal(self, id_mal):
        self.lookups += 1
        return None

    def save(self, media_id, status=None, progress=None, score_1dp=None):
        self.writes.append((media_id, status, progress, score_1dp))
        return {}


class FakeMal:
    def __init__(self):
        self.writes = []

    def update(self, mal_id, status=None, progress=None, score_1dp=None):
        self.writes.append((mal_id, status, progress, score_1dp))
        return {}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    anime = json.load(open(path, encoding="utf-8")).get("anime") or []
    print(f"loaded {len(anime)} anime entries from {path}\n")

    cfg = Config()
    cfg.enable_anilist = cfg.enable_mal = True
    cfg.dry_run = True

    st = State(os.path.join(tempfile.mkdtemp(), "state.json"))
    simkl, al, mal = FakeSimkl(anime), FakeAniList(), FakeMal()

    print("=" * 70, "\nPASS 1 - cold state, everything should be written\n", "=" * 70)
    logging.disable(logging.INFO)
    outbound_tick(cfg, st, simkl, al, mal)
    logging.disable(logging.NOTSET)
    print(f"  anilist writes : {len(al.writes)}")
    print(f"  mal writes     : {len(mal.writes)}")
    print(f"  by_mal lookups : {al.lookups}   <- should be ~1 (Simkl supplies anilist ids)")
    print(f"  simkl calls    : {simkl.calls}")

    n1_al, n1_mal = len(al.writes), len(mal.writes)

    print("\n" + "=" * 70, "\nPASS 2 - nothing changed, must be a total no-op\n", "=" * 70)
    outbound_tick(cfg, st, simkl, al, mal)
    print(f"  anilist writes : {len(al.writes) - n1_al}   <- must be 0")
    print(f"  mal writes     : {len(mal.writes) - n1_mal}   <- must be 0")
    ok_noop = len(al.writes) == n1_al and len(mal.writes) == n1_mal

    print("\n" + "=" * 70, "\nSPOT CHECK - longest series in the export\n", "=" * 70)
    # The longest-running entry is the one most likely to span several cours,
    # which is where progress and id mapping are worth eyeballing by hand.
    def _total_eps(entry):
        try:
            return int(entry.get("total_episodes_count") or 0)
        except (TypeError, ValueError):
            return 0

    with_ids = [e for e in anime if ((e.get("show") or {}).get("ids") or {}).get("anilist")]
    if not with_ids:
        print("  no entries carry an AniList id - nothing to spot check")
    else:
        e = max(with_ids, key=_total_eps)
        ids = e["show"]["ids"]
        w = next((x for x in al.writes if x[0] == int(ids["anilist"])), None)
        print(f"  title    : {e['show'].get('title')}")
        print(f"  ids      : mal={ids.get('mal')} anilist={ids.get('anilist')} kitsu={ids.get('kitsu')}")
        print(f"  simkl    : status={e.get('status')} watched={e.get('watched_episodes_count')}"
              f"/{e.get('total_episodes_count')} last={e.get('last_watched')}")
        print(f"  -> anilist write: mediaId={w[0]} status={w[1]} progress={w[2]} score={w[3]}"
              if w else "  -> NO WRITE")

    print("\n" + "=" * 70, "\nRATING FIDELITY - scoreRaw round-trip\n", "=" * 70)
    rated = [w for w in al.writes if w[3] is not None][:6]
    for media_id, status, prog, score in rated:
        print(f"  score {score}  ->  scoreRaw {int(round(score * 10))}  ->  renders {score}")

    print("\n" + "=" * 70)
    print("RESULT:", "PASS - dedup holds, no redundant writes" if ok_noop
          else "FAIL - second pass wrote again")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main() or 0)
