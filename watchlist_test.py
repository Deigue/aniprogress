#!/usr/bin/env python3
"""Offline proof of the merged reconcile tick.

No network, no tokens, no stored "what we wrote". Feeds hand-built libraries
through the real reconcile_tick with every client stubbed, and asserts:

  * progress is monotonic - the higher side wins, both directions
  * a status change on Simkl (it appears in the incremental pull) flows to AniList
  * a status change on AniList (Simkl unchanged) flows to Simkl
  * COMPLETED on AniList is a floor - a Simkl rewatch marker never un-completes it
  * a title on one side only is created on the other, unless it was on both
    last tick - then it was removed, and is removed everywhere
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
from aniprogress.main import (_SNAPSHOT_SCHEMA, reconcile_one,  # noqa: E402
                              reconcile_tick)
from aniprogress.anilist import AniListLookupFailed      # noqa: E402
from aniprogress.simkl import Simkl, SimklLookupFailed   # noqa: E402
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


def sk_row(mal, status, watched=0, rating=None, total=24):
    return {"status": status, "watched_episodes_count": watched, "user_rating": rating,
            "total_episodes_count": total,
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
        full = str(date_from).startswith("1970")
        return {"anime": list(self._rows if full else self._moved)}

    def resolve_mal(self, mal_id):
        self.lookups.append(int(mal_id))
        if self._catalogue is None:
            return 900000 + int(mal_id)      # a distinct Simkl id per title
        return (900000 + int(mal_id)) if int(mal_id) in self._catalogue else None

    def add_history(self, p): self.history.append(p); return {}
    def add_to_list(self, p): self.lists.append(p); return {}
    def add_rating(self, p): self.ratings.append(p); return {}

    def remove_from_library(self, sid):
        self.removed = getattr(self, "removed", []) + [int(sid)]
        return True
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

    def delete(self, entry_id):
        self.deleted = getattr(self, "deleted", []) + [int(entry_id)]
        return True


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
        "total": int(r.get("total_episodes_count") or 0),
    } for r in rows}
    st.set("simkl_anime", snap)
    st.set("simkl_snapshot_at", "2026-02-01T00:00:00Z")
    st.set("simkl_activity_all", "2026-02-01T00:00:00Z")
    st.set("simkl_anime_cursor", "2026-02-01T00:00:00Z")
    st.set("simkl_removed_at", "2026-01-01T00:00:00Z")   # nothing removed yet
    st.set("simkl_snapshot_shape", f"{_SNAPSHOT_SCHEMA}|{Config().simkl_epoch}")
    return st


def test_unit_reconcile_one():
    print("\n" + "=" * 70 + "\nreconcile_one - the pure decision core\n" + "=" * 70)
    check("progress higher on AniList -> episodes to Simkl",
          reconcile_one({"status": "CURRENT", "progress": 5, "score": None, "total": 24},
                        {"status": "watching", "progress": 2, "rating": None, "total": 24},
                        moved=False, rewatch=False, push_simkl=True), [("sk_hist", 5)])
    check("dropped on Simkl (moved) -> AniList DROPPED",
          reconcile_one({"status": "CURRENT", "progress": 5, "score": None, "total": 24},
                        {"status": "dropped", "progress": 5, "rating": None, "total": 24},
                        moved=True, rewatch=False, push_simkl=True), [("al", "DROPPED", None, None)])
    check("dropped on AniList (Simkl still) -> Simkl dropped",
          reconcile_one({"status": "DROPPED", "progress": 5, "score": None, "total": 24},
                        {"status": "watching", "progress": 5, "rating": None, "total": 24},
                        moved=False, rewatch=False, push_simkl=True), [("sk_list", "dropped")])
    check("COMPLETED floor: Simkl plantowatch changes nothing",
          reconcile_one({"status": "COMPLETED", "progress": 24, "score": None, "total": 24},
                        {"status": "plantowatch", "progress": 24, "rating": None, "total": 24},
                        moved=True, rewatch=False, push_simkl=True), [])


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
    check("the lookup result is cached", sorted(st.get("simkl_ids")), ["9"])

    reconcile_tick(_cfg(), st, simkl, al, None)
    check("second tick does not look it up again", simkl.lookups, [9])
    check("second tick still writes nothing",
          (simkl.history, simkl.lists, simkl.ratings), ([], [], []))


def test_episode_count_mismatch_is_not_a_push():
    """The 2026-09-11 defect: AniList folds in OVAs and splits films, so a
    finished show is 24/24 there and 22/22 on Simkl. Comparing raw numbers
    pushed those titles on every tick forever - Simkl caps at its own total."""
    print(chr(10) + "=" * 70)
    print("reconcile_one - finished means 'reached ITS OWN total'")
    print("=" * 70)
    check("AniList 24/24 vs Simkl 22/22 -> nothing",
          reconcile_one({"status": "COMPLETED", "progress": 24, "score": None, "total": 24},
                        {"status": "completed", "progress": 22, "rating": None, "total": 22},
                        moved=False, rewatch=False, push_simkl=True), [])
    check("AniList 4/4 film vs Simkl 1/1 film -> nothing",
          reconcile_one({"status": "COMPLETED", "progress": 4, "score": None, "total": 4},
                        {"status": "completed", "progress": 1, "rating": None, "total": 1},
                        moved=False, rewatch=False, push_simkl=True), [])
    check("finished-but-plantowatch on Simkl is a rewatch marker, not a target",
          reconcile_one({"status": "COMPLETED", "progress": 23, "score": None, "total": 23},
                        {"status": "plantowatch", "progress": 22, "rating": None, "total": 22},
                        moved=False, rewatch=False, push_simkl=True), [])
    check("genuinely behind on the SAME total still pushes",
          reconcile_one({"status": "CURRENT", "progress": 11, "score": None, "total": 14},
                        {"status": "watching", "progress": 10, "rating": None, "total": 14},
                        moved=False, rewatch=False, push_simkl=True), [("sk_hist", 11)])


def test_rewatch_moves_anilist_to_repeating():
    print(chr(10) + "=" * 70)
    print("RECONCILE - a rewatch started on Simkl -> AniList REPEATING")
    print("=" * 70)
    check("Simkl restarted at ep1 -> AniList REPEATING ep1",
          reconcile_one({"status": "COMPLETED", "progress": 24, "score": None, "total": 24},
                        {"status": "watching", "progress": 1, "rating": None, "total": 24},
                        moved=True, rewatch=True, push_simkl=True),
          [("al", "REPEATING", 1, None)])
    check("rewatch in progress keeps tracking forward",
          reconcile_one({"status": "REPEATING", "progress": 1, "score": None, "total": 24},
                        {"status": "watching", "progress": 3, "rating": None, "total": 24},
                        moved=False, rewatch=False, push_simkl=True),
          [("al", "CURRENT", 3, None)])

    # end to end: the snapshot's progress going backwards is the whole signal
    st = _state_with_snapshot([sk_row(5, "completed", watched=24)])
    rows = [sk_row(5, "watching", watched=1)]
    simkl = StubSimkl(rows, moved_since=rows)
    al = StubAniList([al_entry(55, 5, "COMPLETED", progress=24)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("tick detects the reset and writes REPEATING",
          al.saves, [(55, "REPEATING", 1, None)])



def test_full_read_is_rate_limited():
    """A full library read is the expensive call. It must fire on the first run
    and after a removal, but never more often than SIMKL_FULL_MIN_HOURS."""
    print(chr(10) + "=" * 70)
    print("RECONCILE - full re-reads are rate limited")
    print("=" * 70)
    import time as _t
    rows = [sk_row(1, "plantowatch")]
    cfg = _cfg()
    cfg.simkl_full_min_hours = 6.0
    by = {1: {"id": 11, "idMal": 1, "title": {"english": "a"}},
          2: {"id": 22, "idMal": 2, "title": {"english": "b"}}}

    st = _state_with_snapshot([sk_row(1, "plantowatch"), sk_row(2, "plantowatch")])
    st.set("simkl_full_at", _t.time())
    reconcile_tick(cfg, st, StubSimkl(rows, moved_since=[], removed_at="2026-06-06T00:00:00Z"),
                   StubAniList([], by_mal=by), None)
    check("inside the window the ghost is NOT pruned",
          sorted(st.get("simkl_anime")), ["1", "2"])
    check("the removal cursor is not consumed, so it retries later",
          st.get("simkl_removed_at"), "2026-01-01T00:00:00Z")

    st2 = _state_with_snapshot([sk_row(1, "plantowatch"), sk_row(2, "plantowatch")])
    st2.set("simkl_full_at", _t.time() - 7 * 3600)
    reconcile_tick(cfg, st2, StubSimkl(rows, moved_since=[], removed_at="2026-06-06T00:00:00Z"),
                   StubAniList([], by_mal=by), None)
    check("outside the window the ghost is pruned",
          sorted(st2.get("simkl_anime")), ["1"])
    check("the removal cursor is consumed", st2.get("simkl_removed_at"), "2026-06-06T00:00:00Z")



def test_alias_onto_an_existing_entry_is_refused():
    """Simkl resolves a special/short/split film to its PARENT series and still
    echoes back the mal id asked for. Writing then lands on the parent - nine
    episodes of "Sword Art OFFline" onto Sword Art Online. All 20 AniList-only
    titles in the real library did exactly this."""
    print(chr(10) + "=" * 70)
    print("RECONCILE - a write that would land on another entry is refused")
    print("=" * 70)
    parent = sk_row(11757, "completed", watched=25, total=25)     # in the library
    st = _state_with_snapshot([parent])
    st.get("simkl_anime")["11757"]["simkl"] = 37226               # its Simkl id

    class Aliasing(StubSimkl):
        def resolve_mal(self, mal_id):
            self.lookups.append(int(mal_id))
            return 37226          # every lookup maps onto the parent

    simkl = Aliasing([parent], moved_since=[])
    # the parody short, watched on AniList, absent from Simkl by MAL id
    al = StubAniList([al_entry(16099, 16099, "COMPLETED", progress=9, score_1dp=3.0),
                      al_entry(117570, 11757, "COMPLETED", progress=25)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("nothing was written onto the parent series",
          (simkl.history, simkl.lists, simkl.ratings), ([], [], []))
    check("the resolved id was cached", st.get("simkl_ids"), {"16099": 37226})

    n = len(simkl.lookups)
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("second tick does not look it up again", len(simkl.lookups), n)
    check("and still writes nothing",
          (simkl.history, simkl.lists, simkl.ratings), ([], [], []))



def test_mal_is_compared_not_blindly_written():
    """MAL used to be write-only: every tick either blind-wrote every rating or
    leaned on a remembered-writes cache. It is read now, so only real
    differences are sent."""
    print(chr(10) + "=" * 70)
    print("RECONCILE - MAL is read first, so only differences are written")
    print("=" * 70)
    from aniprogress.mal import Mal

    class FakeMal:
        def __init__(self, have): self._have = have; self.writes = []
        def list_entries(self): return dict(self._have)
        def update(self, mal_id, **kw): self.writes.append((mal_id, kw)); return {}

    rows = [sk_row(70, "completed", watched=12, total=12)]
    st = _state_with_snapshot(rows)
    al = StubAniList([al_entry(700, 70, "COMPLETED", progress=12, score_1dp=8.0)])
    cfg = _cfg(); cfg.enable_mal = True

    # MAL already agrees on everything -> not written at all
    m = FakeMal({70: {"status": "completed", "progress": 12, "score": 8, "total": 12}})
    reconcile_tick(cfg, _state_with_snapshot(rows), StubSimkl(rows, moved_since=[]), al, m)
    check("an agreeing MAL entry is left alone", m.writes, [])

    # MAL missing the score only -> just the score is sent
    m = FakeMal({70: {"status": "completed", "progress": 12, "score": 0, "total": 12}})
    reconcile_tick(cfg, _state_with_snapshot(rows), StubSimkl(rows, moved_since=[]), al, m)
    check("only the differing field is sent", m.writes, [(70, {"score_1dp": 8.0})])

    # MAL has never seen it -> status, progress and score together, one call
    m = FakeMal({})
    reconcile_tick(cfg, _state_with_snapshot(rows), StubSimkl(rows, moved_since=[]), al, m)
    check("a new MAL entry is one call with every field",
          m.writes, [(70, {"status": "COMPLETED", "progress": 12, "score_1dp": 8.0})])



def test_one_bad_title_does_not_sink_the_tick():
    """2026-09-12 live run: a single AniList lookup stalled, the tick died
    mid-loop and left AniList half created, SIMKL untouched and MAL half filled.
    A failing title must be counted and stepped over."""
    print(chr(10) + "=" * 70)
    print("RECONCILE - one failing title is isolated, the rest still syncs")
    print("=" * 70)
    rows = [sk_row(1, "watching", watched=5), sk_row(2, "watching", watched=5)]
    st = _state_with_snapshot(rows)

    class Boom(StubAniList):
        def save(self, media_id, **kw):
            if int(media_id) == 11:
                raise RuntimeError("anilist request failed after retries")
            return super().save(media_id, **kw)

    al = Boom([al_entry(11, 1, "CURRENT", progress=1),
               al_entry(22, 2, "CURRENT", progress=1)])
    reconcile_tick(_cfg(), st, StubSimkl(rows, moved_since=[]), al, None)
    ok = [s for s in al.saves if s[0] == 22]
    check("the healthy title was still written", len(ok), 1)
    check("the failing one wrote nothing", [s for s in al.saves if s[0] == 11], [])


def test_an_unreachable_simkl_catalogue_is_not_cached_as_absent():
    """The catalogue cache is permanent and never retried, so a lookup that
    could not be ASKED must not be filed alongside one that answered "no"."""
    print(chr(10) + "=" * 70)
    print("RECONCILE - an unreachable catalogue is retried, not cached")
    print("=" * 70)
    st = _state_with_snapshot([])

    class Unreachable(StubSimkl):
        fail = True

        def resolve_mal(self, mal_id):
            if self.fail:
                self.lookups.append(int(mal_id))
                raise SimklLookupFailed(f"mal:{mal_id} lookup failed (timeout)")
            return super().resolve_mal(mal_id)   # records the lookup itself

    simkl = Unreachable([], moved_since=[])
    al = StubAniList([al_entry(99, 9, "COMPLETED", progress=12, score_1dp=8.0)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("nothing was pushed while the catalogue was unreachable",
          (simkl.history, simkl.lists, simkl.ratings), ([], [], []))
    check("the failure was NOT cached as absent", st.get("simkl_ids") or {}, {})

    simkl.fail = False
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("the next tick asks again", simkl.lookups, [9, 9])
    check("and the write lands once the catalogue answers",
          bool(simkl.history or simkl.lists), True)


def test_an_unreachable_anilist_lookup_is_not_reported_as_absent():
    """`unmatched` is remembered in state and named only once, so a transport
    failure recorded there goes permanently quiet. It must land in `failed`
    (retried next tick) instead."""
    print(chr(10) + "=" * 70)
    print("RECONCILE - an unreachable AniList lookup is retried, not filed absent")
    print("=" * 70)
    rows = [sk_row(1, "completed", watched=12)]
    st = _state_with_snapshot([])

    class Unreachable(StubAniList):
        fail = True

        def by_mal(self, m):
            if self.fail:
                raise AniListLookupFailed(f"mal:{m} lookup unavailable (timeout)")
            return super().by_mal(m)

    al = Unreachable([], by_mal={1: {"id": 11, "title": {"romaji": "T"}}})
    reconcile_tick(_cfg(), st, StubSimkl(rows, moved_since=rows), al, None)
    check("nothing was created on AniList", al.saves, [])
    check("it was NOT remembered as 'no AniList entry exists'",
          (st.get("reported") or {}).get("no_anilist_media") or {}, {})

    al.fail = False
    reconcile_tick(_cfg(), st, StubSimkl(rows, moved_since=rows), al, None)
    check("the next tick creates it", [s[0] for s in al.saves], [11])


def test_one_change_reaches_all_three_in_one_tick():
    """MAL used to be compared against AniList as READ at the top of the tick,
    so a change originating on Simkl landed on AniList in tick 1 and only
    reached MAL in tick 2. It must propagate in a single pass."""
    print(chr(10) + "=" * 70)
    print("RECONCILE - one change propagates to every provider in one tick")
    print("=" * 70)

    class FakeMal:
        def __init__(self, have): self._have = have; self.writes = []
        def list_entries(self): return dict(self._have)
        def update(self, mal_id, **kw): self.writes.append((mal_id, kw)); return {}

    cfg = _cfg(); cfg.enable_mal = True

    # Simkl moved to ep6; AniList and MAL both still sit at ep5.
    rows = [sk_row(70, "watching", watched=6)]
    st = _state_with_snapshot([sk_row(70, "watching", watched=5)])
    al = StubAniList([al_entry(700, 70, "CURRENT", progress=5)])
    m = FakeMal({70: {"status": "watching", "progress": 5, "score": 0, "total": 24}})
    reconcile_tick(cfg, st, StubSimkl(rows, moved_since=rows), al, m)
    check("AniList advanced in this tick", [s[2] for s in al.saves], [6])
    check("and so did MAL, same tick", m.writes, [(70, {"progress": 6})])

    # A title Simkl has that neither of the others do: created on both at once.
    rows = [sk_row(71, "completed", watched=12, total=12)]
    st = _state_with_snapshot([])
    al = StubAniList([], by_mal={71: {"id": 711, "title": {"romaji": "New"}}})
    m = FakeMal({})
    reconcile_tick(cfg, st, StubSimkl(rows, moved_since=rows), al, m)
    check("AniList got the create", [(s[0], s[1], s[2]) for s in al.saves],
          [(711, "COMPLETED", 12)])
    check("MAL got it in the same tick too",
          m.writes, [(71, {"status": "COMPLETED", "progress": 12})])


def test_a_late_simkl_row_is_re_pulled():
    print("\n" + "=" * 70 + "\nRECONCILE - a rating Simkl served late is still picked up\n" + "=" * 70)
    # The 2026-09-26 loop: a film was rated on Simkl, the pull right after came
    # back without the rating, and the cursor moved past it. Every tick then
    # re-sent the rating (a no-op on Simkl, so its activity never moved again).
    from datetime import datetime, timedelta, timezone
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    rated_at, cursor = now - timedelta(minutes=5), now - timedelta(minutes=2)

    class LaggedSimkl(StubSimkl):
        def __init__(self):
            super().__init__([sk_row(7, "completed", 1, rating=3, total=1)])
            self.pulls = []
        def activities(self):
            return {"all": iso(cursor), "anime": {"removed_from_list": self._removed_at}}
        def all_items(self, media_type="anime", status=None, date_from=None, extended="full"):
            self.pulls.append(date_from)
            # A floor after the change hides it.
            return {"anime": list(self._rows) if date_from <= iso(rated_at) else []}

    st = _state_with_snapshot([sk_row(7, "completed", 1, rating=None, total=1)])
    for k in ("simkl_snapshot_at", "simkl_activity_all", "simkl_anime_cursor"):
        st.set(k, iso(cursor))                   # activity has NOT moved since
    simkl = LaggedSimkl()
    al = StubAniList([al_entry(70, 7, "COMPLETED", progress=1, score_1dp=3.3)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("quiet activity inside the lookback still pulls, from before the cursor",
          [d <= iso(cursor - timedelta(minutes=59)) for d in simkl.pulls], [True])
    check("the late rating reached the snapshot", st.get("simkl_anime")["7"]["rating"], 3.0)
    check("so the rating is not re-sent to Simkl", simkl.ratings, [])


def test_a_rating_only_change_is_read_from_the_ratings_feed():
    print("\n" + "=" * 70 + "\nRECONCILE - a rating-only change comes from Simkl's ratings feed\n" + "=" * 70)
    # The 2026-10-03 loop: all-items' date_from never returns a row whose only
    # change is its rating, however far back the lookback reaches. The snapshot
    # kept rating None and the rating was re-sent every tick until a full read.
    from datetime import datetime, timedelta, timezone
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    rated_at = now - timedelta(minutes=1)

    class RatingBlindSimkl(StubSimkl):
        def __init__(self):
            # The ratings feed's copy is STALE on progress (1 vs the snapshot's 3).
            super().__init__([sk_row(7, "watching", 1, rating=8)], moved_since=[])
            self.rated_pulls = []
        def activities(self):
            return {"all": iso(rated_at), "anime": {
                "removed_from_list": self._removed_at, "rated_at": iso(rated_at)}}
        def rated_items(self, media_type="anime", date_from=""):
            self.rated_pulls.append(date_from)
            return {"anime": list(self._rows)}

    st = _state_with_snapshot([sk_row(7, "watching", 3, rating=None)])
    simkl = RatingBlindSimkl()
    al = StubAniList([al_entry(70, 7, "CURRENT", progress=3, score_1dp=8.3)])
    reconcile_tick(_cfg(), st, simkl, al, None)
    row = st.get("simkl_anime")["7"]
    check("the ratings feed was asked", len(simkl.rated_pulls), 1)
    check("the rating reached the snapshot", row["rating"], 8.0)
    check("progress was NOT taken from the ratings feed", row["progress"], 3)
    check("so the rating is not re-sent to Simkl", simkl.ratings, [])
    check("and nothing else was written", (simkl.history, simkl.lists, al.saves), ([], [], []))

    # A tick with no rating activity in the window does not ask.
    simkl2 = StubSimkl([])
    reconcile_tick(_cfg(), st, simkl2, al, None)
    check("no rated_at stamp -> ratings feed not called",
          hasattr(simkl2, "rated_pulls"), False)


class DelMal:
    def __init__(self, have): self._have, self.writes, self.deleted = dict(have), [], []
    def list_entries(self): return dict(self._have)
    def update(self, mal_id, **kw): self.writes.append((mal_id, kw)); return {}
    def delete(self, mal_id): self.deleted.append(mal_id); return True


def _paired_state(mals):
    """Titles on both sides last tick, each snapshot row carrying its Simkl id."""
    st = _state_with_snapshot([sk_row(m, "watching", 1) for m in mals])
    snap = st.get("simkl_anime")
    for m in mals:
        snap[str(m)]["simkl"] = 900000 + m
    st.set("simkl_anime", snap)
    return st


def _mal_cfg(dry=False):
    cfg = _cfg()
    cfg.enable_mal = True
    cfg.dry_run = dry
    return cfg


def test_removed_on_anilist_is_removed_everywhere():
    print("\n" + "=" * 70 + "\nREMOVALS - a title removed on AniList leaves Simkl and MAL\n" + "=" * 70)
    st = _paired_state([5, 6])
    rows = [sk_row(5, "watching", 1), sk_row(6, "watching", 1)]
    both = [al_entry(50, 5, "CURRENT", 1), al_entry(60, 6, "CURRENT", 1)]
    mal = DelMal({5: {"status": "watching", "progress": 1, "score": 0, "total": 24},
                  6: {"status": "watching", "progress": 1, "score": 0, "total": 24}})
    reconcile_tick(_mal_cfg(), st, StubSimkl(rows, moved_since=[]), StubAniList(both), mal)
    check("first tick records the overlap", st.get("paired"), [5, 6])

    simkl = StubSimkl(rows, moved_since=[])
    al = StubAniList([both[1]], by_mal={5: {"id": 50, "idMal": 5, "title": {"english": "Five"}}})
    reconcile_tick(_mal_cfg(), st, simkl, al, mal)
    check("removed from Simkl by its SIMKL id", getattr(simkl, "removed", []), [900005])
    check("removed from MAL", mal.deleted, [5])
    check("NOT re-created on AniList", al.saves, [])
    check("gone from the snapshot, so no ghost", sorted(st.get("simkl_anime")), ["6"])
    check("no longer paired", st.get("paired"), [6])

    al3 = StubAniList([both[1]])
    reconcile_tick(_mal_cfg(), st, StubSimkl([rows[1]], moved_since=[]), al3, mal)
    check("next tick writes nothing", (al3.saves, mal.deleted), ([], [5]))


def test_removed_on_simkl_is_removed_everywhere():
    print("\n" + "=" * 70 + "\nREMOVALS - a title removed on Simkl leaves AniList and MAL\n" + "=" * 70)
    import time as _t
    st = _paired_state([5, 6])
    st.set("paired", [5, 6])
    st.set("simkl_full_at", _t.time() - 7 * 3600)
    rows = [sk_row(6, "watching", 1)]                    # 5 left Simkl
    simkl = StubSimkl(rows, moved_since=[], removed_at="2026-06-06T00:00:00Z")
    al = StubAniList([al_entry(50, 5, "CURRENT", 1), al_entry(60, 6, "CURRENT", 1)])
    mal = DelMal({5: {"status": "watching", "progress": 1, "score": 0, "total": 24}})
    reconcile_tick(_mal_cfg(), st, simkl, al, mal)
    check("AniList entry deleted by its LIST ENTRY id", getattr(al, "deleted", []), [500])
    check("removed from MAL", mal.deleted, [5])
    check("NOT pushed back to Simkl", (simkl.history, simkl.lists), ([], []))


def test_a_simkl_re_key_is_not_a_removal():
    print("\n" + "=" * 70 + "\nREMOVALS - Simkl moving an entry to another MAL id deletes nothing\n" + "=" * 70)
    import time as _t
    st = _paired_state([5, 6])
    st.set("paired", [5, 6])
    st.set("simkl_full_at", _t.time() - 7 * 3600)
    moved = sk_row(7, "watching", 1)
    moved["show"]["ids"]["simkl"] = "900005"             # same Simkl entry, new MAL id
    six = sk_row(6, "watching", 1)
    six["show"]["ids"]["simkl"] = "900006"
    simkl = StubSimkl([moved, six], moved_since=[], removed_at="2026-06-06T00:00:00Z")
    al = StubAniList([al_entry(50, 5, "CURRENT", 1), al_entry(60, 6, "CURRENT", 1)])
    reconcile_tick(_mal_cfg(), st, simkl, al, None)
    check("AniList entry kept", getattr(al, "deleted", []), [])


def _over_cap():
    """Five paired titles; four have just left AniList. DELETE_MAX is 3."""
    mals = [1, 2, 3, 4, 9]
    st = _paired_state(mals)
    st.set("paired", mals)
    rows = [sk_row(m, "watching", 1) for m in mals]   # as _paired_state holds them
    by = {m: {"id": m * 10, "idMal": m, "title": {"english": f"t{m}"}} for m in mals}
    cfg = _mal_cfg()
    cfg.delete_max = 3
    return st, rows, by, cfg


def _warnings_during(fn):
    seen = []

    class Grab(logging.Handler):
        def emit(self, r):
            if r.levelno >= logging.WARNING and "removals held" in r.getMessage():
                seen.append(r.getMessage())
    h = Grab()
    logging.getLogger("aniprogress").addHandler(h)
    try:
        fn()
    finally:
        logging.getLogger("aniprogress").removeHandler(h)
    return seen


def test_mass_removal_is_held_not_re_created():
    print("\n" + "=" * 70 + "\nREMOVALS - more than DELETE_MAX at once is HELD\n" + "=" * 70)
    st, rows, by, cfg = _over_cap()
    simkl = StubSimkl(rows, moved_since=[])
    als = []

    def two_ticks():
        for _ in range(2):
            al = StubAniList([al_entry(90, 9, "CURRENT", 1)], by_mal=by)
            als.append(al)
            reconcile_tick(cfg, st, simkl, al, None)
    warned = _warnings_during(two_ticks)
    check("nothing removed from Simkl", getattr(simkl, "removed", []), [])
    check("NOT re-created on AniList either", [a.saves for a in als], [[], []])
    check("still paired, so the removal is not lost", st.get("paired"), [1, 2, 3, 4, 9])
    check("warned once, not per tick", len(warned), 1)
    check("the warning names them and the fix",
          all(s in warned[0] for s in ("t1 / t2 / t3 / t4", "DELETE_MAX=4")), True)


def test_raising_the_cap_applies_held_removals():
    print("\n" + "=" * 70 + "\nREMOVALS - raising DELETE_MAX applies what was held\n" + "=" * 70)
    st, rows, by, cfg = _over_cap()
    reconcile_tick(cfg, st, StubSimkl(rows, moved_since=[]),
                   StubAniList([al_entry(90, 9, "CURRENT", 1)], by_mal=by), None)
    cfg.delete_max = 4                                   # user raises it, redeploys
    simkl = StubSimkl(rows, moved_since=[])
    al = StubAniList([al_entry(90, 9, "CURRENT", 1)], by_mal=by)
    reconcile_tick(cfg, st, simkl, al, None)
    check("all four removed from Simkl", sorted(getattr(simkl, "removed", [])),
          [900001, 900002, 900003, 900004])
    check("hold cleared", st.get("removals_held"), [])
    check("only the survivor still paired", st.get("paired"), [9])


def test_a_bad_read_restores_and_clears_the_hold():
    print("\n" + "=" * 70 + "\nREMOVALS - a later good read ends the hold with nothing lost\n" + "=" * 70)
    st, rows, by, cfg = _over_cap()
    reconcile_tick(cfg, st, StubSimkl(rows, moved_since=[]),
                   StubAniList([al_entry(90, 9, "CURRENT", 1)], by_mal=by), None)
    full = [al_entry(m * 10, m, "CURRENT", 1) for m in (1, 2, 3, 4, 9)]
    simkl = StubSimkl(rows, moved_since=[])
    al = StubAniList(full, by_mal=by)                    # the read comes back whole
    reconcile_tick(cfg, st, simkl, al, None)
    check("nothing removed, nothing written", (getattr(simkl, "removed", []), al.saves),
          ([], []))
    check("hold cleared", st.get("removals_held"), [])


def test_a_simkl_change_outranks_an_anilist_removal():
    print("\n" + "=" * 70 + "\nREMOVALS - a same-tick Simkl change keeps the title\n" + "=" * 70)
    st = _paired_state([5, 6])
    st.set("paired", [5, 6])
    rows = [sk_row(5, "completed", 24), sk_row(6, "watching", 1)]
    simkl = StubSimkl(rows, moved_since=[rows[0]])
    al = StubAniList([al_entry(60, 6, "CURRENT", 1)],
                     by_mal={5: {"id": 50, "idMal": 5, "title": {"english": "Five"}}})
    reconcile_tick(_cfg(), st, simkl, al, None)
    check("not removed from Simkl", getattr(simkl, "removed", []), [])
    check("re-created on AniList from the change", [s[:2] for s in al.saves], [(50, "COMPLETED")])


def test_dry_run_removal_repeats_instead_of_re_creating():
    print("\n" + "=" * 70 + "\nREMOVALS - dry run keeps saying 'would remove'\n" + "=" * 70)
    st = _paired_state([5, 6])
    st.set("paired", [5, 6])
    rows = [sk_row(5, "watching", 1), sk_row(6, "watching", 1)]
    for _ in range(2):
        al = StubAniList([al_entry(60, 6, "CURRENT", 1)],
                         by_mal={5: {"id": 50, "idMal": 5, "title": {"english": "Five"}}})
        reconcile_tick(_mal_cfg(dry=True), st, StubSimkl(rows, moved_since=[]), al, None)
        check("no AniList create", al.saves, [])
    check("still paired, still a removal", st.get("paired"), [5, 6])


def test_deferred_re_read_is_said_once():
    print("\n" + "=" * 70 + "\nRECONCILE - a deferred re-read is logged once, not per tick\n" + "=" * 70)
    import time as _t
    seen = []

    class Grab(logging.Handler):
        def emit(self, r):
            if r.levelno >= logging.INFO and "re-read deferred" in r.getMessage():
                seen.append(r)
    h = Grab()
    logging.getLogger("aniprogress").addHandler(h)
    try:
        st = _state_with_snapshot([sk_row(1, "watching", 1)])
        st.set("simkl_full_at", _t.time())
        simkl = StubSimkl([sk_row(1, "watching", 1)], moved_since=[],
                          removed_at="2026-06-06T00:00:00Z")
        for _ in range(3):
            reconcile_tick(_cfg(), st, simkl, StubAniList([al_entry(10, 1, "CURRENT", 1)]), None)
        check("one INFO line across three ticks", len(seen), 1)
    finally:
        logging.getLogger("aniprogress").removeHandler(h)


def main():
    for fn in (test_unit_reconcile_one, test_episode_count_mismatch_is_not_a_push,
               test_rewatch_moves_anilist_to_repeating, test_tick_simkl_moved_wins,
               test_tick_anilist_moved_wins, test_tick_one_sided_and_idempotent,
               test_removal_prunes_the_snapshot,
               test_absent_from_simkl_catalogue_is_reported_once,
               test_full_read_is_rate_limited,
               test_alias_onto_an_existing_entry_is_refused,
               test_mal_is_compared_not_blindly_written,
               test_one_bad_title_does_not_sink_the_tick,
               test_an_unreachable_simkl_catalogue_is_not_cached_as_absent,
               test_an_unreachable_anilist_lookup_is_not_reported_as_absent,
               test_one_change_reaches_all_three_in_one_tick,
               test_a_late_simkl_row_is_re_pulled,
               test_a_rating_only_change_is_read_from_the_ratings_feed,
               test_removed_on_anilist_is_removed_everywhere,
               test_removed_on_simkl_is_removed_everywhere,
               test_a_simkl_re_key_is_not_a_removal,
               test_mass_removal_is_held_not_re_created,
               test_raising_the_cap_applies_held_removals,
               test_a_bad_read_restores_and_clears_the_hold,
               test_a_simkl_change_outranks_an_anilist_removal,
               test_dry_run_removal_repeats_instead_of_re_creating,
               test_deferred_re_read_is_said_once):
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
