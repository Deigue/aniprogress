"""aniprogress — anime progress and ratings across Simkl, AniList, MAL and Floppy.

Exists because nothing off the shelf writes episode progress to AniList.

  RECONCILE   Simkl <-> AniList, one pass, both directions. Progress, watchlist
              status, and gap-fill ratings. Reads each side once; the Simkl
              incremental pull says which side of a status disagreement moved.
  RATINGS     Floppy <-> AniList, both ways, 1dp preserved.

There is no stored record of "what we wrote" - the two live libraries are the
only truth, and that comparison is the dedup. The only persisted state is the
Simkl snapshot (Simkl bans repeated full reads) and the ratings recency pair.

Simkl and MAL store whole numbers, so a rating only ever fills an empty slot
there and is never read back to overwrite AniList's decimal.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from .anilist import STATUS_MAP as AL_STATUS
from .anilist import AniList
from .config import Config, load_dotenv
from .floppy import Floppy
from .mal import Mal
from .simkl import Simkl
from .state import State

log = logging.getLogger("aniprogress")
_stop = threading.Event()

# One tick at a time, so RECONCILE and RATINGS never splice their multi-line
# log blocks together (Python serialises single records, not groups of them).
_tick_lock = threading.Lock()


def _score_1dp(raw) -> float | None:
    """Simkl ratings are integers; treat them as N.0 so comparisons are stable."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if v > 0 else None


def _progress_of(entry: dict) -> int:
    """Simkl's anime entries carry watched_episodes_count on every row
    (verified against a full export: 240/240). The others are legacy fallbacks."""
    for key in ("watched_episodes_count", "last_watched_episode", "episodes_watched"):
        v = entry.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, dict) and isinstance(v.get("episode"), int):
            return v["episode"]
    return 0


# AniList MediaListStatus <-> Simkl list vocabulary. Every watchlist status
# syncs, in both directions - there is no "ladder", a change is a change.
AL_TO_SIMKL = {
    "CURRENT": "watching", "REPEATING": "watching",
    "PLANNING": "plantowatch", "PAUSED": "hold",
    "DROPPED": "dropped", "COMPLETED": "completed",
}


def reconcile_one(al: dict | None, sk: dict | None, *,
                  moved: bool, push_simkl: bool) -> list[tuple]:
    """Decide every write for ONE title from the two live states. Pure.

    `al` = {"status","progress","score"}  (AniList vocab, score 1dp or None)
    `sk` = {"status","progress","rating"} (Simkl list vocab, rating int or None)
    `moved` = this title came back in Simkl's incremental pull this tick, i.e.
              Simkl is the side that just changed it.

    Returns a list of intent tuples:
      ("al_new",  status, progress, score)                  create on AniList
      ("al",      status|None, progress|None, score|None)   update AniList
      ("sk_hist", progress)                                 push episodes 1..N
      ("sk_list", simkl_status)                             set Simkl list status
      ("sk_rate", int)                                      set Simkl rating

    No stored "what we wrote": the two live libraries are the only truth, and
    once a write lands they agree and the next tick is a no-op.
    """
    out: list[tuple] = []
    if al is None and sk is None:
        return out

    # -- one side only: mirror it onto the other ---------------------------
    if al is None:
        s = AL_STATUS.get((sk["status"] or "").lower())
        out.append(("al_new", s, int(sk["progress"] or 0), _score_1dp(sk["rating"])))
        return out
    if sk is None:
        if push_simkl:
            if int(al["progress"] or 0) > 0:
                out.append(("sk_hist", int(al["progress"])))
            else:
                t = AL_TO_SIMKL.get(al["status"])
                if t:
                    out.append(("sk_list", t))
            if al["score"] is not None:
                out.append(("sk_rate", max(1, min(10, round(al["score"])))))
        return out

    al_status, al_prog = al["status"], int(al["progress"] or 0)
    sk_status = AL_STATUS.get((sk["status"] or "").lower())
    sk_prog = int(sk["progress"] or 0)

    # COMPLETED on AniList is a hard floor. Whatever Simkl shows - plan-to-watch,
    # "watching" at ep1, a lower episode count - is either a deliberate rewatch
    # marker or a stale row, and we cannot tell which from the data. So progress
    # and status are BOTH left alone; a genuine rewatch flowing back as REPEATING
    # needs Simkl's rewatch fields, which are not wired up yet (TODO). Only a
    # missing rating is still filled.
    if al_status != "COMPLETED":
        # -- progress: monotonic, higher wins ----------------------------
        if al_prog > sk_prog:
            if push_simkl:
                out.append(("sk_hist", al_prog))
        elif sk_prog > al_prog:
            out.append(("al", sk_status, sk_prog, None))
        elif sk_status and sk_status != al_status:
            # -- equal progress: arbitrate the status by who just moved --
            if sk_status == "COMPLETED":
                out.append(("al", "COMPLETED", None, None))
            elif moved:
                out.append(("al", sk_status, None, None))     # Simkl just changed
            elif push_simkl:
                t = AL_TO_SIMKL.get(al_status)                # AniList is the mover
                if t:
                    out.append(("sk_list", t))

    # -- rating: fill an empty slot on either side, never overwrite ------
    if al["score"] is not None and sk["rating"] is None and push_simkl:
        out.append(("sk_rate", max(1, min(10, round(al["score"])))))
    elif sk["rating"] is not None and al["score"] is None:
        out.append(("al", None, None, _score_1dp(sk["rating"])))

    return out


def simkl_snapshot(st: State) -> dict[str, dict]:
    """Simkl's anime library as of the last pull, keyed by MAL id.

    Built once from an epoch-anchored read, then kept current from each tick's
    incremental (date_from) pull - Simkl bans repeated full reads, so this is
    the only way reconcile can compare against the *whole* Simkl library every
    tick without re-fetching it. It is a materialised view of Simkl's own data,
    never a record of our decisions.
    """
    snap = st.get("simkl_anime") or {}
    return snap if isinstance(snap, dict) else {}


def update_simkl_snapshot(st: State, entries: list[dict]) -> int:
    """Merge an incremental Simkl pull into the snapshot. Returns rows touched."""
    snap = dict(simkl_snapshot(st))
    for e in entries:
        ids = Simkl.ids_of(e)
        mal_id = _int_or_none(ids.get("mal"))
        if mal_id is None:
            continue
        snap[str(mal_id)] = {
            "progress": _progress_of(e),
            "status": str(e.get("status") or ""),
            "rating": _score_1dp(e.get("user_rating")),
        }
    st.set("simkl_anime", snap)
    return len(entries)


_KEEP = object()


def _snap_write(st: State, mal_id: int, *, progress=_KEEP, status=_KEEP,
                rating=_KEEP) -> None:
    """Fold a write we just made into the Simkl snapshot, so the next reconcile
    does not re-detect it as pending before the incremental pull catches up.

    Merges into the existing row - several writes for one title can land in one
    tick (progress, then rating), and each must keep the others' fields. Pass
    only what changed; omitted fields are left as they were.
    """
    snap = dict(simkl_snapshot(st))
    row = dict(snap.get(str(int(mal_id))) or {})
    if progress is not _KEEP:
        row["progress"] = int(progress)
    if status is not _KEEP:
        row["status"] = status
    if rating is not _KEEP:
        row["rating"] = rating
    snap[str(int(mal_id))] = row
    st.set("simkl_anime", snap)


def _remember(cfg: Config, st: State, target: str, key: str, signature) -> None:
    """Record a write, unless this was a dry run.

    A dry run performs no write, so recording one makes every later tick skip a
    change that never happened - and the log stops reporting pending work.
    """
    if not cfg.dry_run:
        st.mark_written(target, key, signature)


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _al_title(media: dict) -> str:
    t = media.get("title") or {}
    return str(t.get("romaji") or t.get("english") or f"anilist:{media.get('id')}")


# --------------------------------------------------------------------------- #
# RECONCILE: Simkl <-> AniList, one pass, both directions
# --------------------------------------------------------------------------- #
def reconcile_tick(
    cfg: Config, st: State, simkl: Simkl, anilist: AniList | None, mal: Mal | None
) -> None:
    """One reconcile of Simkl and AniList: read each side once, decide every
    write in both directions from `reconcile_one`, execute them. The Simkl
    incremental pull (date_from) doubles as the "which side moved" signal that
    settles a bare status disagreement.
    """
    if not (anilist and cfg.enable_anilist):
        return
    dry = "[DRY-RUN] " if cfg.dry_run else ""
    push_simkl = bool(cfg.enable_simkl_push)

    # --- Simkl: snapshot (full, last known) + incremental (what just moved) ---
    acts = simkl.activities()
    if not acts:
        return
    newest = acts.get("all") or ""
    have_snapshot = bool(st.get("simkl_snapshot_at"))
    simkl_moved: set[int] = set()
    if not have_snapshot or (newest and newest != st.get("simkl_activity_all")):
        date_from = (
            cfg.simkl_epoch if not have_snapshot
            else (st.get("simkl_anime_cursor") or cfg.simkl_epoch)
        )
        if not have_snapshot:
            log.info("%sbuilding first Simkl snapshot from %s", dry, date_from)
        recent = Simkl.anime_entries(simkl.all_items("anime", date_from=date_from))
        if have_snapshot:
            for e in recent:
                m = _int_or_none(Simkl.ids_of(e).get("mal"))
                if m is not None:
                    simkl_moved.add(m)
        update_simkl_snapshot(st, recent)
        st.set("simkl_snapshot_at", newest or date_from)
        if newest:
            st.set("simkl_activity_all", newest)
            st.set("simkl_anime_cursor", newest)
        st.save()

    snap = simkl_snapshot(st)

    # --- AniList: one read ---------------------------------------------------
    try:
        entries = anilist.list_entries()
    except Exception:
        log.exception("%scould not load AniList - skipping this tick", dry)
        return
    al_by_mal: dict[int, dict] = {}
    al_no_mal: list[str] = []
    for e in entries:
        m = _int_or_none((e.get("media") or {}).get("idMal"))
        if m is None:
            if e.get("progress") or e.get("scoreRaw"):
                al_no_mal.append(_al_title(e.get("media") or {}))
            continue
        al_by_mal[m] = e

    def _al_state(e: dict) -> dict:
        raw = e.get("scoreRaw")
        return {
            "status": str(e.get("status") or ""),
            "progress": int(e.get("progress") or 0),
            "score": round(int(raw) / 10.0, 1) if raw else None,
        }

    al_upd: list[str] = []
    al_new: list[str] = []
    sk_prog: list[str] = []
    sk_stat: list[str] = []
    sk_rate: list[str] = []
    unmatched: list[str] = []
    mal_writes: list[str] = []

    for m in sorted(set(snap) | {str(x) for x in al_by_mal}):
        mal_id = _int_or_none(m)
        if mal_id is None:
            continue
        srow = snap.get(str(mal_id))
        al_entry = al_by_mal.get(mal_id)
        al = _al_state(al_entry) if al_entry else None
        sk = None
        if srow is not None:
            sk = {
                "status": str(srow.get("status") or ""),
                "progress": int(srow.get("progress") or 0),
                "rating": srow.get("rating"),
            }
        moved = mal_id in simkl_moved
        intents = reconcile_one(al, sk, moved=moved, push_simkl=push_simkl)
        if not intents:
            continue

        title = (_al_title(al_entry.get("media") or {}) if al_entry
                 else f"mal:{mal_id}")
        media_id = _int_or_none((al_entry or {}).get("media", {}).get("id"))

        for intent in intents:
            kind = intent[0]
            if kind == "al_new":
                _, s, p, sc = intent
                media = anilist.by_mal(mal_id)
                new_id = _int_or_none((media or {}).get("id"))
                if new_id is None:
                    unmatched.append(f"{title} (mal:{mal_id})")
                    break
                if media and (media.get("title") or {}).get("romaji"):
                    title = media["title"]["romaji"]
                anilist.save(new_id, status=s, progress=p, score_1dp=sc)
                al_new.append(f"{title} -> {s or '-'} ep{p}"
                              + (f" @{sc}" if sc is not None else ""))
            elif kind == "al":
                _, s, p, sc = intent
                if media_id is None:
                    continue
                anilist.save(media_id, status=s, progress=p, score_1dp=sc)
                bits = []
                if p is not None:
                    bits.append(f"ep{al['progress']}->ep{p}")
                if s:
                    bits.append(f"{al['status'] or '-'}->{s}")
                if sc is not None:
                    bits.append(f"rating {sc}")
                al_upd.append(f"{title} ({', '.join(bits)})")
            elif kind == "sk_hist":
                _, p = intent
                simkl.add_history({"anime": [{
                    "ids": {"mal": mal_id},
                    "episodes": [{"number": n} for n in range(1, p + 1)],
                }]})
                if not cfg.dry_run:
                    _snap_write(st, mal_id, progress=p,
                                status=AL_TO_SIMKL.get((al or {}).get("status") or "",
                                                       "watching"))
                sk_prog.append(f"{title} (ep{(sk or {}).get('progress', 0)}->ep{p})")
            elif kind == "sk_list":
                _, target = intent
                simkl.add_to_list({"anime": [{"ids": {"mal": mal_id}, "to": target}]})
                if not cfg.dry_run:
                    _snap_write(st, mal_id, status=target,
                                progress=(sk or {}).get("progress", 0))
                was = (sk or {}).get("status") or "new"
                sk_stat.append(f"{title} ({was} -> {target})")
            elif kind == "sk_rate":
                _, r = intent
                simkl.add_rating({"anime": [{"ids": {"mal": mal_id}, "rating": r}]})
                if not cfg.dry_run:
                    _snap_write(st, mal_id, rating=r)
                sk_rate.append(f"{title} (rating -> {r})")
            elif kind == "unmatched":
                unmatched.append(f"{title} (mal:{mal_id})")

        # MAL mirror - write-only, its own dedup cache (no list-read API).
        if mal and cfg.enable_mal and al is not None:
            sig = f"{al['status']}|{al['progress']}|{al['score']}"
            if st.differs("mal", str(mal_id), sig):
                mal.update(mal_id, status=al["status"], progress=al["progress"],
                           score_1dp=al["score"])
                _remember(cfg, st, "mal", str(mal_id), sig)
                mal_writes.append(f"{title} (ep{al['progress']} {al['status']})")

    work = (al_upd, al_new, sk_prog, sk_stat, sk_rate, mal_writes, unmatched)
    if not any(work):
        log.info("%sRECONCILE ok - %d AniList / %d Simkl, in sync", dry,
                 len(al_by_mal), len(snap))
        st.save()
        return
    for t in al_new:
        log.info("%sAL new  %s", dry, t)
    for t in al_upd:
        log.info("%sAL <-   %s", dry, t)
    for t in sk_prog:
        log.info("%sSK <- progress %s", dry, t)
    for t in sk_stat:
        log.info("%sSK <- status   %s", dry, t)
    for t in sk_rate:
        log.info("%sSK <- rating   %s", dry, t)
    for t in mal_writes:
        log.info("%sMAL <-  %s", dry, t)
    for t in unmatched:
        log.info("%s??  no AniList media %s", dry, t)
    for t in al_no_mal:
        log.info("%s??  no MAL id %s", dry, t)
    log.info(
        "%sRECONCILE: AniList %d new / %d updated, Simkl %d progress / %d status / "
        "%d rating, %d unmatched",
        dry, len(al_new), len(al_upd), len(sk_prog), len(sk_stat), len(sk_rate),
        len(unmatched),
    )
    st.save()




# --------------------------------------------------------------------------- #
# RATINGS: Floppy <-> AniList
# --------------------------------------------------------------------------- #
def plan_ratings(
    floppy_scores: dict[int, float],
    anilist_by_mal: dict[int, dict],
    winner: str = "skip",
    seen: dict | None = None,
) -> tuple[list, list, list]:
    """Decide the rating writes for one tick. Pure - no I/O, so it is testable.

    Returns (to_anilist, to_floppy, conflicts) where the first two are lists of
    (mal_id, score) and conflicts is (mal_id, floppy_score, anilist_score, why).

    Filling a gap always happens. When both sides hold a different number:
      * if `seen` (last tick's {mal: [floppy, anilist]}) shows exactly one side
        moved, that side is the newer edit and wins
      * otherwise fall back to `winner` ("skip" -> report, write nothing)
    """
    to_anilist: list[tuple[int, float]] = []
    to_floppy: list[tuple[int, float]] = []
    conflicts: list[tuple[int, float, float, str]] = []
    seen = seen or {}

    for mal_id, entry in anilist_by_mal.items():
        raw = entry.get("scoreRaw")
        a_score = round(int(raw) / 10.0, 1) if raw else None
        f_score = floppy_scores.get(mal_id)

        if f_score is None and a_score is None:
            continue
        if f_score is not None and a_score is not None:
            if abs(f_score - a_score) < 0.05:  # equal at 1dp
                continue
            was = seen.get(str(mal_id)) or seen.get(mal_id)
            decided = None
            if was:
                f0, a0 = was
                f_moved = f0 is None or abs((f0 or 0) - f_score) >= 0.05
                a_moved = a0 is None or abs((a0 or 0) - a_score) >= 0.05
                if f_moved and not a_moved:
                    decided = "floppy"
                elif a_moved and not f_moved:
                    decided = "anilist"
            why = decided or winner
            conflicts.append((mal_id, f_score, a_score, why))
            if why == "floppy":
                to_anilist.append((mal_id, f_score))
            elif why == "anilist":
                to_floppy.append((mal_id, a_score))
            # "skip": report it, write nothing, leave the human to decide
            continue
        if f_score is not None:  # only Floppy has it
            to_anilist.append((mal_id, f_score))
        else:  # only AniList has it
            to_floppy.append((mal_id, a_score))

    # Anything rated in Floppy that AniList has never heard of cannot be written:
    # SaveMediaListEntry needs an AniList mediaId, and resolving one costs a
    # request per title. Left for the caller to log, not silently dropped.
    return to_anilist, to_floppy, conflicts


def ratings_tick(
    cfg: Config, st: State, floppy, anilist: AniList | None, mal: Mal | None = None
) -> None:
    # AniList is required as the comparison partner: it is the only anime
    # tracker with a list-read API here, so it is the only one whose ratings can
    # be compared against Floppy's. MAL rides along as a write-only mirror.
    if not (floppy and anilist and cfg.enable_floppy_ratings and cfg.enable_anilist):
        return

    floppy_scores = floppy.anime_scores()
    entries = anilist.list_entries()
    anilist_by_mal: dict[int, dict] = {}
    for e in entries:
        id_mal = (e.get("media") or {}).get("idMal")
        if id_mal:
            anilist_by_mal[int(id_mal)] = e

    rated_al = sum(1 for e in anilist_by_mal.values() if e.get("scoreRaw"))
    seen = st.get("ratings_seen") or {}
    to_anilist, to_floppy, conflicts = plan_ratings(
        floppy_scores, anilist_by_mal, cfg.ratings_winner, seen
    )
    unmatched = set(floppy_scores) - set(anilist_by_mal)

    # Refresh the "last seen" baseline: a materialised view of both sides now,
    # not a record of our own writes. Next tick uses it to tell which side of a
    # fresh disagreement actually moved.
    new_seen = {}
    for m in set(floppy_scores) | set(anilist_by_mal):
        raw = (anilist_by_mal.get(m) or {}).get("scoreRaw")
        new_seen[str(m)] = [floppy_scores.get(m),
                            round(int(raw) / 10.0, 1) if raw else None]
    st.set("ratings_seen", new_seen)

    dry = "[DRY-RUN] " if cfg.dry_run else ""
    mirror_to_mal = bool(mal and cfg.enable_mal)
    unresolved = [c for c in conflicts if c[3] == "skip"]
    if not (to_anilist or to_floppy or unresolved or mirror_to_mal):
        log.info("%sRATINGS ok - Floppy %d / AniList %d rated, in sync", dry,
                 len(floppy_scores), rated_al)
        st.save()
        return

    wrote = 0
    for mal_id, score in to_anilist:
        media = anilist_by_mal[mal_id].get("media") or {}
        if not media.get("id"):
            continue
        anilist.save(int(media["id"]), score_1dp=score)
        wrote += 1
        why = next((c[3] for c in conflicts if c[0] == mal_id), "gap")
        log.info("%sRATE ->AniList %s = %s (%s)", dry, _al_title(media), score, why)

    for mal_id, score in to_floppy:
        floppy.set_score(mal_id, score)
        wrote += 1
        title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
        why = next((c[3] for c in conflicts if c[0] == mal_id), "gap")
        log.info("%sRATE ->Floppy  %s = %s (%s)", dry, title, score, why)

    if mal and cfg.enable_mal:
        settled = dict(floppy_scores)
        settled.update({m: s for m, s in to_floppy})
        settled.update({m: s for m, s in to_anilist})
        for mal_id, score in sorted(settled.items()):
            entry = anilist_by_mal.get(mal_id)
            title = _al_title((entry or {}).get("media") or {}) if entry else f"mal:{mal_id}"
            mal.update(mal_id, score_1dp=score)
            log.info("%sRATE ->MAL     %s = %s", dry, title, round(score))

    for mal_id, f_score, a_score, _ in unresolved:
        title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
        log.warning("%sRATE DECIDE    %s floppy=%s anilist=%s (wrote neither)",
                    dry, title, f_score, a_score)

    log.info("%sRATINGS: %d written, %d need-decision, %d Floppy-only",
             dry, wrote, len(unresolved), len(unmatched))
    st.save()


# --------------------------------------------------------------------------- #
def _loop(name: str, fn, interval: int) -> None:
    while not _stop.is_set():
        started = time.monotonic()
        try:
            # One tick at a time: Python serialises individual log records but
            # not groups of them, so without this the reconcile and ratings
            # blocks would interleave line by line in the container log.
            with _tick_lock:
                fn()
        except Exception:
            log.exception("%s tick failed", name)
        _stop.wait(max(5.0, interval - (time.monotonic() - started)))


def main() -> int:
    # Local runs read .env; under Docker the environment is already populated
    # and nothing here overrides it.
    n = load_dotenv(os.environ.get("DOTENV_PATH", ".env"))
    cfg = Config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
    )

    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        return 2

    if n:
        log.info("loaded %d values from .env", n)
    log.info("aniprogress starting | %s", cfg.summary())
    if cfg.dry_run:
        log.warning("DRY_RUN is on - every write is logged, none are sent")

    st = State(os.path.join(cfg.state_dir, "state.json"))

    # Say out loud what was carried over. This is a sync service: the trackers
    # are the truth and state is only a cache, so anything remembered here has
    # to be visible when it turns out to be wrong.
    snap = simkl_snapshot(st)
    log.info(
        "state: simkl snapshot %s (%d titles), cursor=%s",
        "built " + str(st.get("simkl_snapshot_at"))
        if st.get("simkl_snapshot_at")
        else "ABSENT",
        len(snap),
        st.get("simkl_anime_cursor") or "-",
    )
    stale = [k for k in ("anilist_updated_at",) if st.get(k) is not None]
    if stale:
        for k in stale:
            st.set(k, None)
        st.save()
        log.info("state: dropped keys from a previous design: %s", ", ".join(stale))

    simkl = Simkl(cfg.simkl_client_id, cfg.simkl_token, dry_run=cfg.dry_run)
    anilist = (
        AniList(cfg.anilist_token, dry_run=cfg.dry_run) if cfg.anilist_token else None
    )
    mal = (
        Mal(
            cfg.mal_client_id,
            cfg.mal_token,
            os.environ.get("MAL_REFRESH_TOKEN", ""),
            dry_run=cfg.dry_run,
        )
        if cfg.enable_mal
        else None
    )
    floppy = (
        Floppy(cfg.floppy_url, cfg.floppy_token, dry_run=cfg.dry_run)
        if cfg.enable_floppy_ratings
        else None
    )

    if anilist:
        v = anilist.viewer()
        fmt = (v.get("mediaListOptions") or {}).get("scoreFormat")
        log.info("anilist user=%s scoreFormat=%s", v.get("name"), fmt)
        if fmt != "POINT_10_DECIMAL":
            log.warning(
                "AniList scoreFormat is %s, not POINT_10_DECIMAL - "
                "decimal ratings will not display correctly. "
                "Change it in AniList settings.",
                fmt,
            )

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())

    threads = [
        threading.Thread(
            target=_loop,
            name="reconcile",
            daemon=True,
            args=(
                "reconcile",
                lambda: reconcile_tick(cfg, st, simkl, anilist, mal),
                cfg.reconcile_seconds,
            ),
        ),
    ]
    if floppy:
        threads.append(
            threading.Thread(
                target=_loop,
                name="ratings",
                daemon=True,
                args=(
                    "ratings",
                    lambda: ratings_tick(cfg, st, floppy, anilist, mal),
                    cfg.ratings_seconds,
                ),
            )
        )
    for t in threads:
        t.start()
    _stop.wait()
    log.info("shutting down")
    st.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
