#!/usr/bin/env python3
"""Offline proof of the Floppy <-> AniList ratings pair.

No network, no tokens, no export file needed. Feeds a hand-built library
through the real decision function and the real tick, with both clients
stubbed, and asserts the outcome of every case that matters:

  * a score only Floppy has     -> written to AniList
  * a score only AniList has    -> written to Floppy
  * the same score on both      -> no write at all
  * different scores            -> the configured winner decides
  * decimals                    -> survive in both directions, unrounded
  * a second identical pass     -> writes nothing

    python ratings_test.py
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aniprogress.config import Config          # noqa: E402
from aniprogress.main import plan_ratings, ratings_tick  # noqa: E402
from aniprogress.state import State            # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"          got  {got!r}")
        print(f"          want {want!r}")
        FAILURES.append(label)


def al_entry(media_id: int, id_mal: int, score_1dp: float | None, title: str) -> dict:
    """An AniList list entry as MediaListCollection returns it."""
    return {
        "id": media_id * 10,
        "status": "CURRENT",
        "progress": 1,
        "scoreRaw": None if score_1dp is None else int(round(score_1dp * 10)),
        "updatedAt": 1_700_000_000,
        "media": {"id": media_id, "idMal": id_mal, "episodes": 12,
                  "title": {"romaji": title, "english": title}},
    }


class FakeFloppy:
    def __init__(self, scores: dict[int, float]):
        self._scores = dict(scores)
        self.writes: list[tuple[int, float]] = []

    def anime_scores(self) -> dict[int, float]:
        return dict(self._scores)

    def set_score(self, mal_id: int, score_1dp_value: float):
        self.writes.append((int(mal_id), round(float(score_1dp_value), 1)))
        return {"stub": True}


class FakeAniList:
    def __init__(self, entries: list[dict]):
        self._entries = entries
        self.writes: list[tuple[int, float]] = []

    def list_entries(self) -> list[dict]:
        return list(self._entries)

    def save(self, media_id: int, status=None, progress=None, score_1dp=None):
        self.writes.append((int(media_id), score_1dp))
        return {"stub": True}


# --------------------------------------------------------------------------- #
# The library under test.
#
#   mal 101  Floppy 8.7, AniList unrated      -> push to AniList
#   mal 102  Floppy unrated, AniList 6.4      -> push to Floppy
#   mal 103  both 9.1                         -> no write
#   mal 104  Floppy 7.8, AniList 7.2          -> conflict
#   mal 105  rated in Floppy, absent AniList  -> unmatched, no write
# --------------------------------------------------------------------------- #
FLOPPY_SCORES = {101: 8.7, 103: 9.1, 104: 7.8, 105: 5.5}
ANILIST_ENTRIES = [
    al_entry(2001, 101, None, "Only Floppy Rated"),
    al_entry(2002, 102, 6.4, "Only AniList Rated"),
    al_entry(2003, 103, 9.1, "Agreed"),
    al_entry(2004, 104, 7.2, "Disputed"),
]
BY_MAL = {int((e["media"]["idMal"])): e for e in ANILIST_ENTRIES}


def test_plan_skip_is_default() -> None:
    print("\n" + "=" * 70 + "\nPLAN - default, a disagreement is reported and NOT written\n" + "=" * 70)
    to_al, to_fl, conflicts = plan_ratings(FLOPPY_SCORES, BY_MAL)   # no winner arg
    check("gap-filling still happens to AniList", sorted(to_al), [(101, 8.7)])
    check("gap-filling still happens to Floppy", sorted(to_fl), [(102, 6.4)])
    check("the disagreement is reported", conflicts, [(104, 7.8, 7.2, "skip")])
    check("the disputed title is written to neither side",
          (104 in dict(to_al), 104 in dict(to_fl)), (False, False))


def test_plan_floppy_wins() -> None:
    print("\n" + "=" * 70 + "\nPLAN - RATINGS_WINNER=floppy resolves it\n" + "=" * 70)
    to_al, to_fl, conflicts = plan_ratings(FLOPPY_SCORES, BY_MAL, "floppy")
    check("push to AniList", sorted(to_al), [(101, 8.7), (104, 7.8)])
    check("push to Floppy", sorted(to_fl), [(102, 6.4)])
    check("one conflict, floppy wins", conflicts, [(104, 7.8, 7.2, "floppy")])
    check("agreed title produces no write", 103 in dict(to_al) or 103 in dict(to_fl), False)
    check("unmatched title produces no write", 105 in dict(to_al) or 105 in dict(to_fl), False)


def test_plan_anilist_wins() -> None:
    print("\n" + "=" * 70 + "\nPLAN - RATINGS_WINNER=anilist flips the disagreement\n" + "=" * 70)
    to_al, to_fl, conflicts = plan_ratings(FLOPPY_SCORES, BY_MAL, "anilist")
    check("push to AniList", sorted(to_al), [(101, 8.7)])
    check("push to Floppy", sorted(to_fl), [(102, 6.4), (104, 7.2)])
    check("one conflict, anilist wins", conflicts, [(104, 7.8, 7.2, "anilist")])


def test_tick_and_idempotence() -> None:
    print("\n" + "=" * 70 + "\nTICK - real writes, then a second identical pass\n" + "=" * 70)
    cfg = Config()
    cfg.enable_anilist = True
    cfg.enable_floppy_ratings = True
    cfg.ratings_winner = "floppy"
    cfg.dry_run = False          # let the stubs record the calls

    st = State(os.path.join(tempfile.mkdtemp(), "state.json"))
    fl, al = FakeFloppy(FLOPPY_SCORES), FakeAniList(ANILIST_ENTRIES)

    ratings_tick(cfg, st, fl, al)
    check("anilist received 2 writes", sorted(al.writes), [(2001, 8.7), (2004, 7.8)])
    check("floppy received 1 write", fl.writes, [(102, 6.4)])

    # Idempotence comes from the data converging, not from a remembered write.
    # There is deliberately no dedup state: a state file can disagree with
    # reality, and did - a dry run recorded writes it never made, after which
    # every later tick reported "nothing to do" for a pending change.
    # So apply the writes to the fakes, exactly as the real services would.
    for media_id, score in al.writes:
        for e in ANILIST_ENTRIES:
            if e["media"]["id"] == media_id:
                e["scoreRaw"] = int(round(score * 10))
    for mal_id, score in fl.writes:
        fl._scores[mal_id] = score

    n_al, n_fl = len(al.writes), len(fl.writes)
    ratings_tick(cfg, st, fl, al)
    check("once both sides agree, anilist gets nothing", len(al.writes) - n_al, 0)
    check("once both sides agree, floppy gets nothing", len(fl.writes) - n_fl, 0)

    # And the converse: an unresolved gap must be re-reported every tick, not
    # silently swallowed because a previous tick already saw it.
    fl2, al2 = FakeFloppy({601: 9.0}), FakeAniList([al_entry(6001, 601, None, "Gap")])
    st2 = State(os.path.join(tempfile.mkdtemp(), "state.json"))
    ratings_tick(cfg, st2, fl2, al2)
    first = len(al2.writes)
    ratings_tick(cfg, st2, fl2, al2)
    check("an unapplied change is reported again next tick", len(al2.writes), first * 2)


def test_decimals_survive() -> None:
    print("\n" + "=" * 70 + "\nDECIMALS - nothing is rounded in either direction\n" + "=" * 70)
    for value in (8.7, 7.2, 6.4, 9.9, 0.1):
        entry = al_entry(3000, 301, value, "Round Trip")
        back = round(int(entry["scoreRaw"]) / 10.0, 1)
        check(f"anilist round trip {value}", back, value)

    to_al, _, _ = plan_ratings({401: 7.8}, {401: al_entry(4001, 401, None, "x")}, "floppy")
    check("floppy 7.8 reaches anilist unrounded", to_al, [(401, 7.8)])
    _, to_fl, _ = plan_ratings({}, {402: al_entry(4002, 402, 6.3, "y")}, "floppy")
    check("anilist 6.3 reaches floppy unrounded", to_fl, [(402, 6.3)])


def test_unrated_is_not_zero() -> None:
    print("\n" + "=" * 70 + "\nUNRATED - absence must not be written as 0\n" + "=" * 70)
    from aniprogress.floppy import score_1dp
    check("floppy score 0 reads as unrated", score_1dp(0), None)
    check("floppy score None reads as unrated", score_1dp(None), None)
    check("floppy score '' reads as unrated", score_1dp(""), None)
    check("floppy score 7.8 reads as 7.8", score_1dp(7.8), 7.8)
    to_al, to_fl, _ = plan_ratings({}, {501: al_entry(5001, 501, None, "unrated both")}, "floppy")
    check("unrated on both sides writes nothing", (to_al, to_fl), ([], []))


def test_reconcile_ratings_gapfill() -> None:
    """reconcile_one moves a rating only into an empty slot, never over one."""
    print("\n" + "=" * 70 + "\nRECONCILE - rating gap-fill both ways, no overwrite\n" + "=" * 70)
    from aniprogress.main import reconcile_one

    # AniList rated, Simkl empty -> rounded to Simkl
    al = {"status": "COMPLETED", "progress": 12, "score": 8.4, "total": 24}
    sk = {"status": "completed", "progress": 12, "rating": None, "total": 24}
    check("AniList decimal -> Simkl rounded",
          reconcile_one(al, sk, moved=False, rewatch=False, push_simkl=True), [("sk_rate", 8)])

    # Simkl rated, AniList empty -> fills AniList (as 1dp of the int)
    al = {"status": "COMPLETED", "progress": 12, "score": None, "total": 24}
    sk = {"status": "completed", "progress": 12, "rating": 9, "total": 24}
    check("Simkl int -> empty AniList slot",
          reconcile_one(al, sk, moved=False, rewatch=False, push_simkl=True),
          [("al", None, None, 9.0)])

    # Both hold a score -> never touched (AniList keeps its decimal)
    al = {"status": "COMPLETED", "progress": 12, "score": 5.5, "total": 24}
    sk = {"status": "completed", "progress": 12, "rating": 9, "total": 24}
    check("both rated -> nothing", reconcile_one(al, sk, moved=False, rewatch=False, push_simkl=True), [])


def test_reconcile_progress_and_status() -> None:
    print("\n" + "=" * 70 + "\nRECONCILE - progress monotonic, status by who moved\n" + "=" * 70)
    from aniprogress.main import reconcile_one

    # AniList ahead on progress -> explicit episode list to Simkl
    al = {"status": "CURRENT", "progress": 5, "score": None, "total": 24}
    sk = {"status": "watching", "progress": 2, "rating": None, "total": 24}
    check("AniList ep5 > Simkl ep2 -> sk_hist 5",
          reconcile_one(al, sk, moved=False, rewatch=False, push_simkl=True), [("sk_hist", 5)])

    # Simkl ahead on progress -> AniList gets progress + derived status
    al = {"status": "CURRENT", "progress": 2, "score": None, "total": 24}
    sk = {"status": "completed", "progress": 12, "rating": None, "total": 24}
    check("Simkl ep12 > AniList ep2 -> al COMPLETED/12",
          reconcile_one(al, sk, moved=False, rewatch=False, push_simkl=True),
          [("al", "COMPLETED", 12, None)])

    # Equal progress, status differs, Simkl moved -> Simkl wins onto AniList
    al = {"status": "CURRENT", "progress": 5, "score": None, "total": 24}
    sk = {"status": "dropped", "progress": 5, "rating": None, "total": 24}
    check("dropped on Simkl (moved) -> AniList DROPPED",
          reconcile_one(al, sk, moved=True, rewatch=False, push_simkl=True),
          [("al", "DROPPED", None, None)])

    # Equal progress, status differs, Simkl did NOT move -> AniList wins onto Simkl
    check("paused on AniList (Simkl still) -> Simkl hold",
          reconcile_one({"status": "PAUSED", "progress": 5, "score": None, "total": 24},
                        {"status": "watching", "progress": 5, "rating": None, "total": 24},
                        moved=False, rewatch=False, push_simkl=True),
          [("sk_list", "hold")])


def test_reconcile_completed_floor() -> None:
    print("\n" + "=" * 70 + "\nRECONCILE - COMPLETED on AniList is a hard floor\n" + "=" * 70)
    from aniprogress.main import reconcile_one

    # Simkl plan-to-watch over a completed AniList entry (rewatch marker) -> nothing
    check("Simkl plantowatch does not un-complete AniList",
          reconcile_one({"status": "COMPLETED", "progress": 37, "score": None, "total": 24},
                        {"status": "plantowatch", "progress": 37, "rating": None, "total": 24},
                        moved=True, rewatch=False, push_simkl=True),
          [])
    # ...even when Simkl shows a lower episode count
    check("Simkl watching/ep1 does not un-complete AniList",
          reconcile_one({"status": "COMPLETED", "progress": 12, "score": None, "total": 24},
                        {"status": "watching", "progress": 1, "rating": None, "total": 24},
                        moved=True, rewatch=False, push_simkl=True),
          [])


def test_reconcile_one_sided() -> None:
    print("\n" + "=" * 70 + "\nRECONCILE - a title on one side only is mirrored\n" + "=" * 70)
    from aniprogress.main import reconcile_one

    # On AniList only, watched -> pushed to Simkl as explicit history
    check("AniList-only completed -> sk_hist",
          reconcile_one({"status": "COMPLETED", "progress": 12, "score": 8.0, "total": 24}, None,
                        moved=False, rewatch=False, push_simkl=True),
          [("sk_hist", 12), ("sk_list", "completed"), ("sk_rate", 8)])
    # On AniList only, plan-to-watch -> add_to_list
    check("AniList-only planning -> sk_list plantowatch",
          reconcile_one({"status": "PLANNING", "progress": 0, "score": None, "total": 24}, None,
                        moved=False, rewatch=False, push_simkl=True),
          [("sk_list", "plantowatch")])
    # On Simkl only -> create on AniList mirroring it
    check("Simkl-only completed -> al_new",
          reconcile_one(None, {"status": "completed", "progress": 12, "rating": 7, "total": 24},
                        moved=False, rewatch=False, push_simkl=True),
          [("al_new", "COMPLETED", 12, 7.0)])




def test_a_failing_rating_write_does_not_abort_the_tick() -> None:
    """2026-09-12 live: a Floppy PATCH that HAD been applied raised after two
    timeouts and a 500. The tick died, the write was never logged, and the two
    ratings behind it were never attempted."""
    print(chr(10) + "=" * 70)
    print("RATINGS - one unconfirmed write is isolated, the rest still go")
    print("=" * 70)
    cfg = Config()
    cfg.enable_anilist = cfg.enable_floppy_ratings = True
    cfg.ratings_winner = "anilist"
    cfg.dry_run = False

    class FlakyFloppy(FakeFloppy):
        def set_score(self, mal_id, score_1dp_value):
            if int(mal_id) == 701:
                raise RuntimeError("timed out or errored on every attempt")
            return super().set_score(mal_id, score_1dp_value)

    entries = [al_entry(7001, 701, 6.2, "Flaky"),
               al_entry(7002, 702, 7.0, "Fine"),
               al_entry(7003, 703, 8.0, "Also fine")]
    fl = FlakyFloppy({701: 6.1, 702: 7.1, 703: 8.1})
    al = FakeAniList(entries)
    st = State(os.path.join(tempfile.mkdtemp(), "state.json"))
    ratings_tick(cfg, st, fl, al)
    check("the two healthy writes still landed",
          sorted(m for m, _ in fl.writes), [702, 703])
    check("the failing one wrote nothing", [w for w in fl.writes if w[0] == 701], [])

def main() -> int:
    for fn in (test_plan_skip_is_default, test_plan_floppy_wins, test_plan_anilist_wins,
               test_tick_and_idempotence,
               test_decimals_survive, test_unrated_is_not_zero,
               test_reconcile_ratings_gapfill, test_reconcile_progress_and_status,
               test_reconcile_completed_floor, test_reconcile_one_sided,
               test_a_failing_rating_write_does_not_abort_the_tick):
        fn()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RESULT: FAIL - {len(FAILURES)} check(s) failed")
        for f in FAILURES:
            print(f"  - {f}")
    else:
        print("RESULT: PASS - ratings move both ways, decimals intact, repeats free")
    print("=" * 70)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
