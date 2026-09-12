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
from .mal import STATUS_MAP as MAL_STATUS
from .mal import Mal
from .simkl import Simkl, SimklLookupFailed
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


def reconcile_one(al: dict | None, sk: dict | None, *, moved: bool,
                  rewatch: bool, push_simkl: bool) -> list[tuple]:
    """Decide every write for ONE title from the two live states. Pure.

    `al` = {"status","progress","score","total"}   AniList vocab, score 1dp
    `sk` = {"status","progress","rating","total"}  Simkl list vocab, rating int
    `moved`   = Simkl's status changed this tick, so Simkl is the side that moved.
    `rewatch` = Simkl's progress went BACKWARDS this tick. Progress does not
                un-happen, so that only means a rewatch was started there.

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
            # History AND the status. Simkl derives a status from the episodes it
            # is given - one episode of a show you dropped reads as "watching" -
            # and the next tick would copy that derived status back over AniList's
            # real one. Stating the status explicitly closes that loop.
            if int(al["progress"] or 0) > 0:
                out.append(("sk_hist", int(al["progress"])))
            t = AL_TO_SIMKL.get(al["status"])
            if t:
                out.append(("sk_list", t))
            if al["score"] is not None:
                out.append(("sk_rate", max(1, min(10, round(al["score"])))))
        return out

    al_status, al_prog = al["status"], int(al["progress"] or 0)
    sk_status = AL_STATUS.get((sk["status"] or "").lower())
    sk_prog = int(sk["progress"] or 0)

    # "Finished" means a side reached ITS OWN episode count, never that the two
    # numbers match. AniList folds in OVAs and splits films into parts, so the
    # same finished show is 24/24 there and 22/22 on Simkl. Comparing the raw
    # numbers pushed those five titles on every tick forever, because Simkl
    # caps at its own total and the gap can never close.
    al_done = bool(al["total"]) and al_prog >= int(al["total"])
    sk_done = bool(sk["total"]) and sk_prog >= int(sk["total"])

    # A rewatch restarting on Simkl is the one thing that moves a finished
    # AniList entry: progress dropped, so mirror it as REPEATING at Simkl's
    # episode. This has to be tested before the finished-guard below, which
    # would otherwise hold the entry at COMPLETED forever.
    if (rewatch and sk_prog < al_prog
            and sk_status in ("CURRENT", "REPEATING")):
        out.append(("al", "REPEATING" if al_done or al_status == "COMPLETED"
                    else sk_status, sk_prog, None))

    # A finished side has seen the whole show. Never push progress at one (the
    # numbers cannot converge, and a finished-but-plan-to-watch row on Simkl is
    # a deliberate rewatch marker), and never pull progress from Simkl into a
    # finished AniList entry.
    elif al_status != "COMPLETED" and not al_done:
        if al_prog > sk_prog:
            if push_simkl and not sk_done:
                out.append(("sk_hist", al_prog))
                # Same reason as above: say the status too when Simkl's would not
                # match, so pushing episodes of a dropped or paused show does not
                # come back as "watching".
                t = AL_TO_SIMKL.get(al_status)
                if t and t != (sk["status"] or ""):
                    out.append(("sk_list", t))
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
    elif sk_done and sk_status == "COMPLETED" and al_status != "COMPLETED":
        # Both finished, Simkl says so explicitly and AniList has not caught up.
        out.append(("al", "COMPLETED", None, None))

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


# Bump whenever a snapshot row gains or changes a field. The snapshot is only
# ever reshaped by a full read, so without this a new field stays absent - and a
# rule that depends on it (the "finished" test needs `total`) silently never
# fires against rows written by an older build.
_SNAPSHOT_SCHEMA = 3


def _rows_of(entries: list[dict]) -> dict[str, dict]:
    """Simkl entries -> snapshot rows keyed by MAL id."""
    rows: dict[str, dict] = {}
    for e in entries:
        mal_id = _int_or_none(Simkl.ids_of(e).get("mal"))
        if mal_id is None:
            continue
        rows[str(mal_id)] = {
            "progress": _progress_of(e),
            "status": str(e.get("status") or ""),
            "rating": _score_1dp(e.get("user_rating")),
            "total": _int_or_none(e.get("total_episodes_count")) or 0,
            "simkl": _int_or_none(Simkl.ids_of(e).get("simkl")) or 0,
        }
    return rows


def update_simkl_snapshot(st: State, entries: list[dict], *,
                          replace: bool = False) -> None:
    """Fold a Simkl pull into the snapshot.

    `replace=True` for a full (epoch-anchored) read: the result IS Simkl's whole
    library, so rows absent from it have been removed and must be dropped.
    Merging a full read instead is what let deleted titles live in the snapshot
    forever and get recreated on AniList every tick.

    `replace=False` for an incremental pull, which only carries what changed -
    absence there means "unchanged", not "gone".
    """
    rows = _rows_of(entries)
    st.set("simkl_anime", rows if replace else {**simkl_snapshot(st), **rows})


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _tally(*groups) -> str:
    """Join "label a b c" segments with " | ", dropping every zero.

    A summary line is a tally, not a form to fill in: "0 updated" is noise, and
    with every provider active the long form no longer fits on one line.
    """
    out = []
    for label, parts in groups:
        kept = [text for count, text in parts if count]
        if kept:
            out.append(f"{label} {' '.join(kept)}" if label else " ".join(kept))
    return "  |  ".join(out)


def _named(title: str, mal_id: int) -> str:
    """Title plus its MAL id, without repeating the id when that IS the title."""
    return title if title == f"mal:{mal_id}" else f"{title} (mal:{mal_id})"


def _grouped(dry: str, prefix: str, items: list[str], width: int = 108) -> None:
    """Log like items on shared lines, " / " separated and wrapped.

    A create or a catalogue fact carries no per-title detail worth a line of its
    own, and forty of them in a column buries the writes that do. Progress,
    status and rating changes keep one line each - those you read individually.
    """
    line = ""
    for item in items:
        nxt = f"{line} / {item}" if line else item
        if line and len(dry) + len(prefix) + len(nxt) + 1 > width:
            log.info("%s%s %s", dry, prefix, line)
            line = item
        else:
            line = nxt
    if line:
        log.info("%s%s %s", dry, prefix, line)


def _al_title(media: dict) -> str:
    """English name first - it is what makes a log line scannable. Romaji is
    the fallback, since AniList leaves `english` null on plenty of entries."""
    t = media.get("title") or {}
    return str(t.get("english") or t.get("romaji") or f"anilist:{media.get('id')}")


# --------------------------------------------------------------------------- #
# RECONCILE: Simkl <-> AniList, one pass, both directions
# --------------------------------------------------------------------------- #
def reconcile_tick(
    cfg: Config, st: State, simkl: Simkl, anilist: AniList | None, mal: Mal | None
) -> None:
    """One reconcile of Simkl and AniList: read each side once, decide every
    write in both directions from `reconcile_one`, execute them. Diffing the
    Simkl snapshot across the pull gives the "which side moved" signal that
    settles a bare status disagreement.
    """
    if not (anilist and cfg.enable_anilist):
        return
    dry = "[DRY-RUN] " if cfg.dry_run else ""
    push_simkl = bool(cfg.enable_simkl_push)

    # --- Simkl ---------------------------------------------------------------
    acts = simkl.activities()
    if not acts:
        return
    newest = acts.get("all") or ""
    removed_at = str((acts.get("anime") or {}).get("removed_from_list") or "")
    have_snapshot = bool(st.get("simkl_snapshot_at"))
    before = simkl_snapshot(st)

    # A removal is invisible to an incremental pull: the row simply stops coming
    # back, and absence cannot be merged. Simkl has no removals feed either -
    # /sync/all-items/anime/removed_from_list is byte-identical to the plain
    # feed, the path segment is ignored - so re-reading the library and
    # REPLACING the snapshot is the only thing that can prune a deleted title.
    #
    # That read is expensive, so it is rate limited. Simkl's rule is about
    # calling all-items WITHOUT a date_from and without checking /sync/activities
    # first; both are always honoured here. What is left is frequency, and
    # SIMKL_FULL_MIN_HOURS keeps it to a handful a day instead of every tick.
    # The snapshot's shape is set by the code that wrote it and the date floor it
    # was read with, so a change to either makes every existing row suspect. That
    # is not "slightly stale", it is wrong, so it re-reads regardless of the rate
    # limit: a 2010 floor had hidden 41 titles, and the fix could not reach the
    # snapshot because a rebuild only ever fired on a removal.
    shape = f"{_SNAPSHOT_SCHEMA}|{cfg.simkl_epoch}"
    reshape = have_snapshot and str(st.get("simkl_snapshot_shape") or "") != shape
    removed = removed_at != str(st.get("simkl_removed_at") or "")
    want_full = (not have_snapshot) or reshape or removed
    since_full = time.time() - float(st.get("simkl_full_at") or 0)
    full = want_full and (not have_snapshot or reshape
                          or since_full >= cfg.simkl_full_min_hours * 3600)
    if want_full and not full:
        log.info("%sa title left a Simkl list; full re-read deferred %.1fh "
                 "(SIMKL_FULL_MIN_HOURS=%s)", dry,
                 (cfg.simkl_full_min_hours * 3600 - since_full) / 3600.0,
                 cfg.simkl_full_min_hours)

    if full or (newest and newest != st.get("simkl_activity_all")):
        date_from = (
            cfg.simkl_epoch if full
            else (st.get("simkl_anime_cursor") or cfg.simkl_epoch)
        )
        if full:
            log.info("%sfull Simkl re-read from %s (%s)", dry, date_from,
                     "first run" if not have_snapshot else
                     "snapshot shape changed" if reshape else "a title left a list")
        rows = Simkl.anime_entries(simkl.all_items("anime", date_from=date_from))
        update_simkl_snapshot(st, rows, replace=full)
        st.set("simkl_snapshot_at", newest or date_from)
        if full:
            # Only record the removal cursor once the prune actually happened,
            # so a deferred removal still fires at the next allowed window.
            st.set("simkl_removed_at", removed_at)
            st.set("simkl_snapshot_shape", shape)
            st.set("simkl_full_at", time.time())
        if newest:
            st.set("simkl_activity_all", newest)
            st.set("simkl_anime_cursor", newest)
        st.save()

    snap = simkl_snapshot(st)

    # Who moved? Diff the snapshot across the pull rather than trusting "it came
    # back in the incremental read" - a row returns for any change at all, and a
    # rating edit is not evidence the STATUS moved. On the first run there is no
    # before-state, so nothing counts as moved.
    # A full read rewrites rows to CORRECT our view - titles it had never seen,
    # values an earlier design had fossilised - so a before/after diff there is
    # not evidence of anything the user did. Only an incremental pull carries
    # that meaning, so the rewatch signal is taken from incremental ticks alone.
    simkl_moved: set[int] = set()
    simkl_rewatch: set[int] = set()
    if have_snapshot and not full:
        for key, row in snap.items():
            prev = before.get(key)
            m = _int_or_none(key)
            if m is None:
                continue
            if prev is None or (prev.get("status") or "") != (row.get("status") or ""):
                simkl_moved.add(m)
            # Episodes watched do not un-happen. Simkl's count going DOWN means
            # the show was restarted there - a rewatch - which is the one signal
            # that may move a finished AniList entry back to REPEATING.
            if prev is not None and int(row.get("progress") or 0) < int(prev.get("progress") or 0):
                simkl_rewatch.add(m)
    if full and have_snapshot:
        gone = len(before) - len([k for k in before if k in snap])
        if gone:
            log.info("%spruned %d title(s) no longer on Simkl", dry, gone)

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

    # MAL is read too, so a write only happens on a real difference. Blind
    # writes every tick, or a remembered-writes cache, are the two things this
    # service exists to not do.
    mal_state: dict[int, dict] = {}
    if mal and cfg.enable_mal:
        try:
            mal_state = mal.list_entries()
        except Exception as e:
            # One clear line, not a traceback every cycle. A rejected token is a
            # standing condition, so say it loudly once and quietly after that -
            # and carry on: MAL being unreachable must not stop the rest syncing.
            first = not getattr(mal, "_read_failed", False)
            mal._read_failed = True
            (log.warning if first else log.debug)(
                "%sMAL unreadable (%s) - mirror skipped. If the token was "
                "rotated, re-run `python -m aniprogress.mal_auth`.", dry, e)
            mal = None

    def _al_state(e: dict) -> dict:
        raw = e.get("scoreRaw")
        return {
            "status": str(e.get("status") or ""),
            "progress": int(e.get("progress") or 0),
            "score": round(int(raw) / 10.0, 1) if raw else None,
            "total": _int_or_none((e.get("media") or {}).get("episodes")) or 0,
        }

    al_upd: list[str] = []
    al_new: list[tuple[str, str]] = []
    sk_writes: dict[int, dict] = {}      # mal -> one condensed line per title
    n_prog = n_stat = n_rate = 0
    unmatched: list[str] = []
    not_in_simkl: list[str] = []
    sk_unresolved: list[str] = []
    aliased: list[str] = []
    failed: list[str] = []
    mal_writes: list[tuple[str, str, str]] = []

    # Does Simkl's catalogue carry this MAL id? A write for one it does not is
    # accepted and silently does nothing, so such a title would be "pending" on
    # every tick forever. Both answers are cached: a miss so it is never retried
    # or re-reported, a hit so the lookup is not repeated every cycle (in dry run
    # the push never lands, so nothing else would ever stop it). This is a fact
    # about Simkl's catalogue, not a record of anything we did, so it is kept in
    # dry run too.
    # MAL id -> the Simkl id a write for it would actually land on (0 = Simkl
    # has no such title). Simkl maps a special, short or split film to its PARENT
    # series, so "Simkl knows this id" is not the question - "does it resolve to
    # something my library does not already hold" is. Nine episodes of "Sword Art
    # OFFline" would otherwise have been written onto Sword Art Online. Cached
    # both ways: it is an immutable fact about Simkl's catalogue, and re-looking
    # up every one of them on every tick is its own problem.
    cat: dict = dict(st.get("simkl_ids") or {})
    cat_new = False
    held = {int(r["simkl"]) for r in snap.values() if r.get("simkl")}

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
                "total": int(srow.get("total") or 0),
            }
        intents = reconcile_one(al, sk, moved=mal_id in simkl_moved,
                                rewatch=mal_id in simkl_rewatch,
                                push_simkl=push_simkl)
        title = (_al_title(al_entry.get("media") or {}) if al_entry
                 else f"mal:{mal_id}")
        media_id = _int_or_none((al_entry or {}).get("media", {}).get("id"))

        try:
            # MAL mirrors AniList. Compare, then write only the fields that differ.
            if mal and cfg.enable_mal and al is not None:
                have = mal_state.get(mal_id)
                want_status = MAL_STATUS.get(al["status"])
                m_status = (have or {}).get("status") or ""
                m_prog = int((have or {}).get("progress") or 0)
                m_score = int((have or {}).get("score") or 0)
                m_total = int((have or {}).get("total") or 0)
                m_done = bool(m_total) and m_prog >= m_total
                fields, bits = {}, []
                if want_status and want_status != m_status:
                    fields["status"] = al["status"]
                    bits.append(f"was {m_status}" if m_status else "new")
                if al["progress"] > m_prog and not m_done:
                    fields["progress"] = al["progress"]
                    bits.append(f"ep{m_prog}->ep{al['progress']}")
                if al["score"] is not None and round(al["score"]) != m_score:
                    fields["score_1dp"] = al["score"]
                    bits.append(f"rating {round(al['score'])}")
                if fields:
                    mal.update(mal_id, **fields)
                    verb = want_status or m_status or "?"
                    mal_writes.append((verb, title, ", ".join(bits)))


        except Exception as e:
            # One title must never take the rest of the run down with it. A
            # stalled AniList lookup did exactly that: AniList half created,
            # Simkl untouched, MAL half filled, and the tick dead mid-loop.
            log.warning("%s!! %s not synced (%s)", dry, title, e)
            failed.append(_named(title, mal_id))

        if not intents:
            continue

        # Everything we would send to Simkl for a title Simkl has never held
        # depends on the catalogue knowing the id. Check once, then drop the
        # Simkl-bound intents for good.
        if sk is None and any(i[0].startswith("sk_") for i in intents):
            target = cat.get(str(mal_id))
            if target is None:
                try:
                    target = simkl.resolve_mal(mal_id) or 0
                except SimklLookupFailed as e:
                    # Not an answer, so nothing is cached - caching a 0 here
                    # would file the title under "not in Simkl, never retried"
                    # permanently. Falling through with 0 drops this tick's
                    # Simkl intents only; the next tick asks again.
                    log.warning("%s?? %s - Simkl catalogue unreachable (%s)",
                                dry, title, e)
                    sk_unresolved.append(_named(title, mal_id))
                    target = 0
                else:
                    cat[str(mal_id)] = target
                    cat_new = True
                    if not target:
                        not_in_simkl.append(_named(title, mal_id))
                    elif int(target) in held:
                        aliased.append(_named(title, mal_id))
            if (not target) or int(target) in held:
                # Either Simkl has no such title, or it resolves to an entry the
                # library already holds under a different MAL id. Writing would
                # land on that other entry, so drop every Simkl-bound intent.
                intents = [i for i in intents if not i[0].startswith("sk_")]
            if not intents:
                continue

        try:
            for intent in intents:
                kind = intent[0]
                if kind == "al_new":
                    _, s, p, sc = intent
                    media = anilist.by_mal(mal_id)
                    new_id = _int_or_none((media or {}).get("id"))
                    if new_id is None:
                        unmatched.append(_named(title, mal_id))
                        break
                    if media:
                        title = _al_title(media)
                    anilist.save(new_id, status=s, progress=p, score_1dp=sc)
                    bits = []
                    if p:
                        bits.append(f"ep{p}")
                    if sc is not None:
                        bits.append(f"@{sc}")
                    al_new.append((s or "-",
                                   f"{title} ({' '.join(bits)})" if bits else title))
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
                # Nothing folds a write back into the snapshot. Recording what we
                # MEANT to send left rows that Simkl never agreed with, and nothing
                # ever corrected them. A write moves Simkl's activity timestamp, so
                # the next tick pulls the real result and the snapshot becomes true
                # rather than hopeful.
                elif kind == "sk_hist":
                    _, p = intent
                    simkl.add_history({"anime": [{
                        "ids": {"mal": mal_id},
                        "episodes": [{"number": n} for n in range(1, p + 1)],
                    }]})
                    w = sk_writes.setdefault(mal_id, {"title": title})
                    w["from"] = int((sk or {}).get("progress", 0)); w["to"] = p
                    n_prog += 1
                elif kind == "sk_list":
                    _, target = intent
                    simkl.add_to_list({"anime": [{"ids": {"mal": mal_id}, "to": target}]})
                    w = sk_writes.setdefault(mal_id, {"title": title})
                    w["status"] = target
                    w["was"] = (sk or {}).get("status") or "new"
                    n_stat += 1
                elif kind == "sk_rate":
                    _, r = intent
                    simkl.add_rating({"anime": [{"ids": {"mal": mal_id}, "rating": r}]})
                    w = sk_writes.setdefault(mal_id, {"title": title})
                    w["rating"] = r
                    n_rate += 1
                elif kind == "unmatched":
                    unmatched.append(_named(title, mal_id))

        except Exception as e:
            # One title must never take the rest of the run down with it. A
            # stalled AniList lookup did exactly that: AniList half created,
            # Simkl untouched, MAL half filled, and the tick dead mid-loop.
            log.warning("%s!! %s not synced (%s)", dry, title, e)
            failed.append(_named(title, mal_id))
    if cat_new:
        st.set("simkl_ids", cat)
    absent = sum(1 for v in cat.values() if not v)
    alias_total = sum(1 for v in cat.values() if v and int(v) in held)

    # Permanent facts about the two catalogues: a title one side simply does not
    # carry cannot change from tick to tick, so name it once and keep only the
    # count thereafter. Repeating them every cycle is what buries the real work.
    said = dict(st.get("reported") or {})

    def _once(kind: str, items: list[str]) -> list[str]:
        seen = dict(said.get(kind) or {})
        fresh = [t for t in items if t not in seen]
        if fresh:
            seen.update({t: True for t in fresh})
            said[kind] = seen
        return fresh

    new_unmatched = _once("no_anilist_media", unmatched)
    new_no_mal = _once("no_mal_id", al_no_mal)
    st.set("reported", said)

    writes = len(al_new) + len(al_upd) + n_prog + n_stat + n_rate
    noise = (new_unmatched + new_no_mal + not_in_simkl + sk_unresolved
             + aliased + failed)
    if not (writes or mal_writes or noise):
        sizes = " / ".join(t for n, t in (
            (len(al_by_mal), f"AniList {len(al_by_mal)}"),
            (len(snap), f"SIMKL {len(snap)}"),
            (len(mal_state), f"MAL {len(mal_state)}")) if n)
        log.info("%sRECONCILE ok - %s, in sync", dry, sizes)
        st.save()
        return
    by_status: dict[str, list[str]] = {}
    for status, label in al_new:
        by_status.setdefault(status, []).append(label)
    for status, names in by_status.items():
        _grouped(dry, f"AL new {status}:", names)
    for t in al_upd:
        log.info("%sAL <-   %s", dry, t)
    for w in sk_writes.values():
        # The status is the verb, so progress, status and rating for one title
        # read as a single sentence instead of three lines that have to be
        # correlated by eye.
        verb = w.get("status") or (w.get("was") or "watching")
        bits = []
        if "to" in w:
            bits.append(f"ep{w['from']}->ep{w['to']}")
        if w.get("status") and w.get("was") not in (None, "new", w["status"]):
            bits.append(f"was {w['was']}")
        if w.get("rating") is not None:
            bits.append(f"rating {w['rating']}")
        detail = f" ({', '.join(bits)})" if bits else ""
        log.info("%sSK <- %-11s %s%s", dry, verb, w["title"], detail)
    for verb, t, detail in mal_writes:
        log.info("%sMAL <- %-13s %s%s", dry, verb, t, f" ({detail})" if detail else "")
    _grouped(dry, "--  not in Simkl's catalogue, never retried:", not_in_simkl)
    _grouped(dry, "--  Simkl maps this onto a title it already holds, skipped:", aliased)
    _grouped(dry, "??  Simkl catalogue unreachable, will retry next tick:",
             sk_unresolved)
    _grouped(dry, "!!  write failed, will retry next tick:", failed)
    _grouped(dry, "--  no AniList entry exists:", new_unmatched)
    _grouped(dry, "--  no MAL id, cannot reach Simkl:", new_no_mal)
    log.info(
        "%sRECONCILE  %s", dry,
        _tally(
            ("AniList", [(len(al_new), f"+{len(al_new)}"),
                         (len(al_upd), f"~{len(al_upd)}")]),
            ("SIMKL", [(n_prog, f"prog{n_prog}"), (n_stat, f"stat{n_stat}"),
                       (n_rate, f"rate{n_rate}")]),
            ("MAL", [(len(mal_writes), str(len(mal_writes)))]),
            ("FAIL", [(len(failed), str(len(failed)))]),
            ("skip", [(absent, f"{absent} not-in-SIMKL"),
                      (alias_total, f"{alias_total} aliased"),
                      (len(unmatched), f"{len(unmatched)} no-AniList"),
                      (len(al_no_mal), f"{len(al_no_mal)} no-MAL-id")]),
        ),
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
    unresolved = [c for c in conflicts if c[3] == "skip"]
    if not (to_anilist or to_floppy or unresolved):
        log.info("%sRATINGS ok - Floppy %d / AniList %d rated, in sync", dry,
                 len(floppy_scores), rated_al)
        st.save()
        return

    # Each write is isolated. One failure used to abort the tick: a Floppy PATCH
    # that had ALREADY been applied raised after two timeouts and a 500, so the
    # log never mentioned it and the two ratings behind it were never attempted.
    n_al = n_fl = 0
    shaky: list[str] = []
    for mal_id, score in to_anilist:
        media = anilist_by_mal[mal_id].get("media") or {}
        if not media.get("id"):
            continue
        why = next((c[3] for c in conflicts if c[0] == mal_id), "gap")
        try:
            anilist.save(int(media["id"]), score_1dp=score)
        except Exception as e:
            log.warning("%sRATE ?? AniList %s = %s did not confirm (%s)",
                        dry, _al_title(media), score, e)
            shaky.append(_al_title(media))
            continue
        n_al += 1
        log.info("%sRATE ->AniList %s = %s (%s)", dry, _al_title(media), score, why)

    for mal_id, score in to_floppy:
        title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
        why = next((c[3] for c in conflicts if c[0] == mal_id), "gap")
        try:
            floppy.set_score(mal_id, score)
        except Exception as e:
            # A timeout is not proof it failed. Say so honestly; the next tick
            # re-reads both sides and either finds agreement or retries.
            log.warning("%sRATE ?? Floppy  %s = %s did not confirm (%s)",
                        dry, title, score, e)
            shaky.append(title)
            continue
        n_fl += 1
        log.info("%sRATE ->Floppy  %s = %s (%s)", dry, title, score, why)

    for mal_id, f_score, a_score, _ in unresolved:
        title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
        log.warning("%sRATE DECIDE    %s floppy=%s anilist=%s (wrote neither)",
                    dry, title, f_score, a_score)

    log.info(
        "%sRATINGS  %s", dry,
        _tally(
            ("AniList", [(n_al, f"~{n_al}")]),
            ("Floppy", [(n_fl, f"~{n_fl}")]),
            ("UNCONFIRMED", [(len(shaky), str(len(shaky)))]),
            ("skip", [(len(unresolved), f"{len(unresolved)} need-decision"),
                      (len(unmatched), f"{len(unmatched)} no-AniList")]),
        ),
    )
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
    # Drop anything a previous design left behind. Every target is now read
    # before it is written, so there is no "already wrote this" record anywhere -
    # that remembered decision is what drifted from reality every time.
    stale = [k for k in ("anilist_updated_at", "written", "simkl_catalogue")
             if st.get(k) is not None]
    for k in stale:
        st.set(k, None)
    if stale:
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
        # Informational only - the username and a scoreFormat warning. AniList's
        # edge throws transient 429s, and letting one of those abort startup
        # killed the container before a single tick had run.
        try:
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
        except Exception as e:
            log.warning("could not read the AniList profile (%s) - carrying on; "
                        "check scoreFormat is POINT_10_DECIMAL yourself", e)

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
