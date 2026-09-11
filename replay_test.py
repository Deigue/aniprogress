#!/usr/bin/env python3
"""Offline proof: replay a real Simkl export through the reconcile logic.

No network, no tokens. Stubs Simkl/AniList/MAL and feeds the anime block of a
Simkl backup through reconcile_tick, so you can see which writes would fire and
confirm that once both sides agree a second pass does nothing.

    python replay_test.py path/to/simkl-export.json

Get the export from Simkl -> Settings -> Import/Export -> Export as JSON.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aniprogress.config import Config           # noqa: E402
from aniprogress.main import reconcile_tick     # noqa: E402
from aniprogress.simkl import Simkl             # noqa: E402
from aniprogress.state import State             # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")


class FakeSimkl:
    def __init__(self, anime):
        self._anime = anime
        self.history, self.lists, self.ratings = [], [], []

    def activities(self):
        return {"all": "2026-09-03T22:17:00Z"}

    def all_items(self, media_type="anime", status=None, date_from=None, extended="full"):
        return {"anime": self._anime}

    def add_history(self, p): self.history.append(p); return {}
    def add_to_list(self, p): self.lists.append(p); return {}
    def add_rating(self, p): self.ratings.append(p); return {}
    anime_entries = staticmethod(Simkl.anime_entries)
    ids_of = staticmethod(Simkl.ids_of)


class FakeAniList:
    """Starts empty, then remembers what reconcile creates so pass 2 can see a
    converged library."""
    def __init__(self):
        self.saves = []
        self._by_id: dict[int, dict] = {}

    def list_entries(self):
        return list(self._by_id.values())

    def by_mal(self, id_mal):
        # Pretend every MAL id resolves to a distinct AniList media id.
        return {"id": 900_000 + int(id_mal), "idMal": int(id_mal),
                "episodes": None, "title": {"romaji": f"mal{id_mal}"}}

    def save(self, media_id, status=None, progress=None, score_1dp=None):
        self.saves.append((media_id, status, progress, score_1dp))
        e = self._by_id.setdefault(int(media_id), {
            "id": int(media_id) * 7, "media": {"id": int(media_id),
            "idMal": int(media_id) - 900_000, "episodes": None,
            "title": {"romaji": f"m{media_id}"}}})
        if status is not None:
            e["status"] = status
        if progress is not None:
            e["progress"] = progress
        if score_1dp is not None:
            e["scoreRaw"] = int(round(score_1dp * 10))
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
    anime = json.load(open(sys.argv[1], encoding="utf-8")).get("anime") or []
    print(f"loaded {len(anime)} anime entries from {sys.argv[1]}\n")

    cfg = Config()
    cfg.enable_anilist = cfg.enable_simkl_push = True
    cfg.enable_mal = False
    cfg.dry_run = False   # fakes only record; state has no write-cache to spoil

    st = State(os.path.join(tempfile.mkdtemp(), "state.json"))
    simkl, al = FakeSimkl(anime), FakeAniList()

    print("=" * 70, "\nPASS 1 - cold AniList, Simkl library replayed in\n", "=" * 70)
    reconcile_tick(cfg, st, simkl, al, None)
    p1 = (len(al.saves), len(simkl.history), len(simkl.lists), len(simkl.ratings))
    print(f"  AniList saves      : {p1[0]}")
    print(f"  Simkl history/list/rating writes : {p1[1]}/{p1[2]}/{p1[3]}")

    print("\n" + "=" * 70, "\nPASS 2 - libraries now agree, must be a no-op\n", "=" * 70)
    reconcile_tick(cfg, st, simkl, al, None)
    p2 = (len(al.saves) - p1[0], len(simkl.history) - p1[1],
          len(simkl.lists) - p1[2], len(simkl.ratings) - p1[3])
    print(f"  new AniList saves  : {p2[0]}   <- want 0")
    print(f"  new Simkl writes   : {p2[1]}/{p2[2]}/{p2[3]}   <- want 0/0/0")

    ok = p2 == (0, 0, 0, 0)
    print("\n" + "=" * 70)
    print("RESULT:", "PASS - converges, second pass is silent" if ok
          else "FAIL - second pass still wrote")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
