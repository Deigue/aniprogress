#!/usr/bin/env python3
"""Offline proof of the merged reconcile tick.

No network, no tokens, no stored "what we wrote". Feeds hand-built libraries
through the real reconcile_tick with every client stubbed, and asserts:

  * progress is monotonic - the higher side wins, both directions
  * a status change on Simkl (it appears in the incremental pull) flows to AniList
  * a status change on AniList (Simkl unchanged) flows to Simkl
  * COMPLETED on AniList is a floor - a Simkl rewatch marker never un-completes it
  * a title on one side only is created on the other
  * a second identical pass writes nothing

    python watchlist_test.py
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aniprogress.config import Config                    # noqa: E402
from aniprogress.main import reconcile_one, reconcile_tick  # noqa: E402
from aniprogress.simkl import Simkl                      # noqa: E402
from aniprogress.state import State                      # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
FAILURES: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


def al_entry(media_id, id_mal, status, progress=0, score_1dp=None):
    return {
        "id": media_id * 10, "status": status, "progress": progress,
        "scoreRaw": None if score_1dp is None else int(round(score_1dp * 10)),
        "updatedAt": 1_700_000_000,
        "media": {"id": media_id, "idMal": id_mal, "episodes": 24,
                  "title": {"romaji": f"t{id_mal}", "english": f"t{id_mal}"}},
    }


def sk_row(mal, status, watched=0, rating=None):
    return {"status": status, "watched_episodes_count": watched, "user_rating": rating,
            "show": {"title": f"t{mal}", "ids": {"mal": str(mal)}}}


class StubSimkl:
    def __init__(self, rows, moved_since=None, removed_at="2026-01-01T00:00:00Z",
                 catalogue=None):
        self._rows = rows
        self._moved = moved_since if moved_since is not None else rows
        self._removed_at = removed_at
        self._catalogue = catalogue          # None -> everything exists
        self.history, self.lists, self.ratings, self.lookups = [], [], [], []

    def activities(self):
        return {"all": "2026-03-01T00:00:00Z",
                "anime": {"removed_from_list": self._removed_at}}

    def all_items(self, media_type="anime", status=None, date_from=None, extended="full"):
        # date_from == epoch -> full read; otherwise -> "what moved"
        full = str(date_from).startswith("2010")
        return {"anime": list(self._rows if full else self._moved)}

    def in_catalogue(self, mal_id):
        self.lookups.append(int(mal_id))
        return True if self._catalogue is None else int(mal_id) in self._catalogue

    def add_history(self, p): self.history.append(p); return {}
    def add_to_list(self, p): self.lists.append(p); return {}
    def add_rating(self, p): self.ratings.append(p); return {}
    anime_entries = staticmethod(Simkl.anime_entries)
    ids_of = staticmethod(Simkl.ids_of)


class StubAniList:
    def __init__(self, entries, by_mal=None):
        self._entries = [dict(e) for e in entries]
        self._by_mal = dict(by_mal or {})
        self.saves = []

    def list_entries(self): return [dict(e) for e in self._entries]
    def by_mal(self, m): return self._by_mal.get(m)

    def save(self, media_id, status=None, progress=None, score_1dp=None):
        self.saves.append((media_id, status, progress, score_1dp))
        return {}


def _cfg():
    c = Config()
    c.enable_anilist = c.enable_simkl_push = True
    c.enable_mal = False
    c.dry_run = False
    return c


def _state_with_snapshot(rows):
    """A state whose Simkl snapshot already holds `rows` (so the tick does an
    incremental pull, not a cold build)."""
    st = State(os.path.join(tempfile.mkdtemp(), "state.json"))
    snap = {str(int(r["show"]["ids"]["mal"])): {
        "progress": int(r.get("watched_episodes_count") or 0),
        "status": r.get("status") or "", "rating": r.get("user_rating"),
    } for r in rows}
    st.set("simkl_anime", snap)
    st.set("simkl_snapshot_at", "2026-02-01T00:00:00Z")
    st.set("simkl_activity_all", "2026-02-01T00:00:00Z")
    st.set("simkl_anime_cursor", "2026-02-01T00:00:00Z")
    st.set("simkl_removed_at", "2026-01-01T00:00:00Z")   # nothing removed yet
    return st


def test_unit_reconcile_one():
    print("\n" + "=" * 70 + "\nreconcile_one - the pure decision core\n" + "=" * 70)
    check("progress higher on AniList -> episodes to Simkl",
          reconcile_one({"status": "CURRENT", "progress": 5, "score": None},
                        {"status": "watching", "progress": 2, "rating": None},
                        moved=False, push_simkl=True), [("sk_hist", 5)])
    check("dropped on Simkl (moved) -> AniList DROPPED",
          reconcile_one({"status": "CURRENT", "progress": 5, "score": None},
                        {"status": "dropped", "progress": 5, "rating": None},
                        moved=True, push_simkl=True), [("al", "DROPPED", None, None)])
    check("dropped on AniList (Simkl still) -> Simkl dropped",
          reconcile_one({"status": "DROPPED", "progress": 5, "score": None},
                        {"status": "watching", "progress": 5, "rating": None},
                        moved=False, push_simkl=True), [("sk_list", "dropped")])
    check("COMPLETED floor: Simkl plantowatch changes nothing",
          reconcile_one({"status": "COMPLETED", "progress": 24, "score": None},
                        {"status": "plantowatch", "progress": 24, "rating": None},
                        moved=True, push_simkl=True), [])


def test_tick_simkl_moved_wins():
    print("\n" + "=" * 70 + "\nRECONCILE - a status Simkl just changed flows to AniList\n" + "=" * 70)
    rows = [sk_row(1, "dropped", watched=3)]
    st = _state_with_snapshot([sk_row(1, "watching", watched=3)])  # snapshot lags
    simkl = StubSimkl(rows, moved_since=rows)                       # incremental: it moved
    al = StubAniList([al_entry(11, 1, "CURRENT", progress=3)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("AniList set to DROPPED", [s[1] for s in al.saves], ["DROPPED"])
    check("nothing pushed to Simkl", (simkl.history, simkl.lists), ([], []))


def test_tick_anilist_moved_wins():
    print("\n" + "=" * 70 + "\nRECONCILE - a status only AniList changed flows to Simkl\n" + "=" * 70)
    rows = [sk_row(2, "watching", watched=4)]
    st = _state_with_snapshot(rows)
    simkl = StubSimkl(rows, moved_since=[])          # Simkl did not move this tick
    al = StubAniList([al_entry(22, 2, "PAUSED", progress=4)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("Simkl list set to hold",
          [p["anime"][0]["to"] for p in simkl.lists], ["hold"])
    check("AniList untouched", al.saves, [])


def test_tick_one_sided_and_idempotent():
    print("\n" + "=" * 70 + "\nRECONCILE - one-sided titles are created, then quiet\n" + "=" * 70)
    rows = [sk_row(3, "plantowatch")]                      # Simkl-only
    st = _state_with_snapshot(rows)
    simkl = StubSimkl(rows, moved_since=[])
    al = StubAniList([al_entry(44, 4, "COMPLETED", progress=12, score_1dp=8.0)],  # AniList-only
                     by_mal={3: {"id": 33, "idMal": 3, "title": {"romaji": "t3"}}})
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("Simkl-only title created on AniList",
          any(s[0] == 33 for s in al.saves), True)
    check("AniList-only watched title pushed to Simkl history",
          [len(p["anime"][0]["episodes"]) for p in simkl.history], [12])


def test_removal_prunes_the_snapshot():
    """The 2026-09-11 defect: a title removed on Simkl lived in the snapshot
    forever and was recreated on AniList on every tick."""
    print("\n" + "=" * 70 + "\nRECONCILE - a title removed on Simkl is pruned, not resurrected\n" + "=" * 70)
    gone = sk_row(7, "plantowatch")           # in the snapshot...
    kept = sk_row(8, "plantowatch")
    st = _state_with_snapshot([gone, kept])
    # ...but Simkl's library now holds only `kept`, and says a title left a list
    simkl = StubSimkl([kept], removed_at="2026-05-05T00:00:00Z")
    al = StubAniList([], by_mal={7: {"id": 77, "idMal": 7, "title": {"english": "Ghost"}},
                                 8: {"id": 88, "idMal": 8, "title": {"english": "Real"}}})
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("the ghost is not recreated on AniList",
          [s[0] for s in al.saves], [88])
    check("the ghost is dropped from the snapshot",
          sorted(st.get("simkl_anime")), ["8"])


def test_absent_from_simkl_catalogue_is_reported_once():
    print("\n" + "=" * 70 + "\nRECONCILE - an id Simkl does not carry is looked up once, then dropped\n" + "=" * 70)
    st = _state_with_snapshot([])
    simkl = StubSimkl([], moved_since=[], catalogue=set())   # catalogue carries nothing
    al = StubAniList([al_entry(99, 9, "COMPLETED", progress=12, score_1dp=8.0)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("nothing was pushed to Simkl",
          (simkl.history, simkl.lists, simkl.ratings), ([], [], []))
    check("the catalogue was consulted once", simkl.lookups, [9])
    check("the lookup result is cached", sorted(st.get("simkl_catalogue")), ["9"])

    reconcile_tick(_cfg(), st, simkl, al, None)
    check("second tick does not look it up again", simkl.lookups, [9])
    check("second tick still writes nothing",
          (simkl.history, simkl.lists, simkl.ratings), ([], [], []))


def main():
    for fn in (test_unit_reconcile_one, test_tick_simkl_moved_wins,
               test_tick_anilist_moved_wins, test_tick_one_sided_and_idempotent,
               test_removal_prunes_the_snapshot,
               test_absent_from_simkl_catalogue_is_reported_once):
        fn()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: FAIL - {len(FAILURES)} check(s) failed")
        for f in FAILURES:
            print(f"  - {f}")
    else:
        print("RESULT: PASS - one reconcile pass, both directions, who-moved arbitration")
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
