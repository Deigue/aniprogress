"""aniprogress — anime progress and ratings across Simkl, AniList, MAL and Floppy.

Exists because nothing off the shelf writes episode progress to AniList.

  OUTBOUND (fast)   to AniList / MAL, from Simkl. Progress and status only.
  INBOUND  (slow)   to Simkl, from AniList. For players that write to AniList.
  RATINGS           Floppy <-> AniList, both ways, 1dp preserved.

Simkl and MAL store whole numbers, so they are written to and never read from
for a rating.
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


# Status ranks. A write may only move an entry UP this ladder, never down:
# Simkl's view of an anime is often staler than AniList's (AniList is where
# Seanime and manual edits land), so an incoming "plantowatch" must never
# overwrite a COMPLETED entry.
_RANK = {"PLANNING": 0, "CURRENT": 1, "PAUSED": 1, "DROPPED": 1, "COMPLETED": 2}


def guard(have: dict | None, status, progress):
    """Clamp a write so it can only improve the target.

    Returns (status, progress, reason), None for fields to leave alone, or None
    entirely when there is nothing worth writing. No rating: Simkl's whole
    numbers must never overwrite an AniList decimal.
    """
    if have is None:  # not on AniList yet - safe to add
        return status, progress, "new"

    cur_status = have.get("status")
    cur_prog = int(have.get("progress") or 0)

    # progress: only ever move forward
    out_prog = progress if progress > cur_prog else None
    # status: only ever move up the ladder
    out_status = status if _RANK.get(status, 0) > _RANK.get(cur_status, 0) else None

    if out_prog is None and out_status is None:
        return None
    return out_status, out_prog, "improve"


def simkl_snapshot(st: State) -> dict[str, dict]:
    """What we last saw in Simkl, keyed by MAL id.

    Built once from an epoch-anchored read and kept current from the same
    incremental pulls OUTBOUND already makes, so nothing ever has to call
    all_items without a date_from. INBOUND diffs against this instead of
    guessing from a cursor, which is what lets it report the exact writes it
    intends rather than pushing the whole list and letting Simkl drop the
    duplicates silently.
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


def _title_of(entry: dict) -> str:
    """Best available human name for a Simkl entry."""
    return str((entry.get("show") or {}).get("title") or "?")


def _al_title(media: dict) -> str:
    t = media.get("title") or {}
    return str(t.get("romaji") or t.get("english") or f"anilist:{media.get('id')}")


def _audit(header: str, lines: list[str], empty: str | None = None) -> None:
    """Log a titled block, or one line when it is empty."""
    if not lines:
        if empty:
            log.info("  %s", empty)
        return
    log.info("%s", header)
    for line in lines:
        log.info("    %s", line)


# --------------------------------------------------------------------------- #
# OUTBOUND: to AniList / MAL
# --------------------------------------------------------------------------- #
def outbound_tick(
    cfg: Config, st: State, simkl: Simkl, anilist: AniList | None, mal: Mal | None
) -> None:
    if not (cfg.enable_anilist or cfg.enable_mal):
        return
    acts = simkl.activities()
    if not acts:
        return
    newest = acts.get("all") or ""
    if newest and newest == st.get("simkl_activity_all"):
        log.info("OUTBOUND to AniList/MAL (no changes found)")
        return

    # Never call all_items without a date_from - Simkl's docs are explicit that
    # it gets the client_id suspended. With no cursor yet, fall back to the
    # configured epoch: still a date_from, and it returns the whole library once
    # so the snapshot below starts complete.
    date_from = st.get("simkl_anime_cursor") or cfg.simkl_epoch
    blob = simkl.all_items("anime", date_from=date_from)
    entries = Simkl.anime_entries(blob)
    if not entries:
        log.info("OUTBOUND to AniList/MAL (no changes found)")
        if newest:
            st.set("simkl_activity_all", newest)
            st.set("simkl_anime_cursor", newest)
            st.save()
        return
    log.info("OUTBOUND to AniList/MAL")
    log.info("  Simkl: %d changed since %s", len(entries), date_from)
    # Keep the shared picture of Simkl current from this same pull, so INBOUND
    # never needs a read of its own.
    update_simkl_snapshot(st, entries)

    # One request: the current AniList state, so no write can regress it.
    current: dict[int, dict] = {}
    if anilist and cfg.enable_anilist:
        try:
            current = {
                int((e.get("media") or {}).get("id") or 0): e
                for e in anilist.list_entries()
            }
            log.info("  AniList: %d tracked", len(current))
        except Exception:
            log.exception(
                "could not load AniList state - skipping this tick rather "
                "than risk overwriting it"
            )
            return

    skipped = 0
    updates: list[str] = []
    adds: list[str] = []
    mal_writes: list[str] = []
    unmatched: list[str] = []

    for e in entries:
        ids = Simkl.ids_of(e)
        # Simkl ships ids as strings and already knows the AniList id for
        # ~100% of anime, so no per-title lookup is needed in the common case.
        mal_id = _int_or_none(ids.get("mal"))
        anilist_id = _int_or_none(ids.get("anilist"))
        if not (mal_id or anilist_id):
            log.debug(
                "skip (no mal/anilist id): %s", (e.get("show") or {}).get("title")
            )
            continue

        status = AL_STATUS.get(str(e.get("status") or "").lower())
        progress = _progress_of(e)
        score = _score_1dp(e.get("user_rating"))
        signature = f"{status}|{progress}|{score}"

        key = str(mal_id or anilist_id)
        if anilist and cfg.enable_anilist and st.differs("anilist", key, signature):
            target = anilist_id
            if target is None:  # rare: Simkl knew no AniList id
                media = anilist.by_mal(mal_id)
                target = media["id"] if media else None
            if target is None:
                unmatched.append(f"{_title_of(e)}  (mal:{mal_id})")
            else:
                # No score: Simkl's whole numbers must never overwrite an
                # AniList decimal. Progress and status only.
                decision = guard(current.get(int(target)), status, progress)
                if decision is None:
                    skipped += 1
                    _remember(cfg, st, "anilist", key, signature)
                    log.debug(
                        "anilist[%s] no-op, target already equal or better | %s",
                        target,
                        (e.get("show") or {}).get("title"),
                    )
                else:
                    g_status, g_prog, why = decision
                    # No score argument: Simkl never rates AniList.
                    anilist.save(target, status=g_status, progress=g_prog)
                    _remember(cfg, st, "anilist", key, signature)
                    have = current.get(int(target)) or {}
                    bits = []
                    if g_prog is not None:
                        bits.append(f"ep{int(have.get('progress') or 0)}->ep{g_prog}")
                    if g_status:
                        bits.append(f"{have.get('status') or '-'}->{g_status}")
                    (adds if why == "new" else updates).append(
                        _title_of(e) + " (" + " ".join(bits) + ")"
                    )

        if mal and cfg.enable_mal and mal_id and st.differs("mal", key, signature):
            mal.update(mal_id, status=status, progress=progress, score_1dp=score)
            _remember(cfg, st, "mal", key, signature)
            note = f" score={round(score)}" if score is not None else ""
            mal_writes.append(f"{_title_of(e)} (ep{progress} {status}{note})")

    if not (updates or adds or mal_writes or unmatched):
        log.info("  nothing to write - %d already equal or better", skipped)
    else:
        _audit("  updating:", updates)
        _audit("  adding:", adds)
        _audit("  mirrored to MAL:", mal_writes)
        _audit("  no AniList entry:", unmatched)
        log.info(
            "  %d updated, %d added, %d unmatched, %d unchanged",
            len(updates), len(adds), len(unmatched), skipped,
        )
    if newest:
        st.set("simkl_activity_all", newest)
        st.set("simkl_anime_cursor", newest)
    st.save()


# --------------------------------------------------------------------------- #
# INBOUND: to Simkl
# --------------------------------------------------------------------------- #
def inbound_tick(cfg: Config, st: State, simkl: Simkl, anilist: AniList | None) -> None:
    if not (anilist and cfg.enable_simkl_push):
        return

    entries = anilist.list_entries()  # ONE request, whole list
    if not entries:
        return
    # Diff against what Simkl actually holds, not against a cursor. The snapshot
    # is maintained by OUTBOUND's incremental pulls, so this costs no Simkl read
    # and reports the exact writes it intends rather than pushing the whole list
    # and letting Simkl drop the duplicates in silence.
    snap = simkl_snapshot(st)
    if not snap:
        log.info(
            "INBOUND from AniList/MAL (waiting - no Simkl snapshot yet, "
            "the next OUTBOUND tick builds it)"
        )
        return

    changed = []
    for e in entries:
        id_mal = (e.get("media") or {}).get("idMal")
        if not id_mal:
            continue
        have = snap.get(str(int(id_mal)))
        if have is None:
            changed.append(e)  # Simkl has never seen it
            continue
        raw = e.get("scoreRaw")
        score = round(int(raw) / 10.0, 1) if raw else None
        if int(e.get("progress") or 0) > int(have.get("progress") or 0):
            changed.append(e)
        elif score is not None and have.get("rating") is None:
            changed.append(e)

    if not changed:
        log.info("INBOUND from AniList/MAL (no changes found)")
        return
    log.info("INBOUND from AniList/MAL")
    log.info(
        "  AniList: %d tracked, Simkl: %d known, %d to push",
        len(entries), len(snap), len(changed),
    )
    pushes: list[str] = []
    no_mal: list[str] = []
    already: int = 0

    for e in changed:
        media = e.get("media") or {}
        mal_id = media.get("idMal")
        if not mal_id:
            # Simkl is keyed on MAL ids, so an AniList-only title has nowhere
            # to go. Nothing is lost on AniList.
            no_mal.append(_al_title(media))
            continue
        progress = int(e.get("progress") or 0)
        raw = e.get("scoreRaw")
        score = round(int(raw) / 10.0, 1) if raw else None
        signature = f"{e.get('status')}|{progress}|{score}"
        if not st.differs("simkl", str(mal_id), signature):
            already += 1
            continue

        payload = {"anime": [{"ids": {"mal": int(mal_id)}}]}
        if progress:
            payload["anime"][0]["episodes"] = [
                {"number": n} for n in range(1, progress + 1)
            ]
        simkl.add_history(payload)
        if score is not None:
            simkl.add_rating(
                {
                    "anime": [
                        {
                            "ids": {"mal": int(mal_id)},
                            "rating": max(1, min(10, round(score))),
                        }
                    ]
                }
            )
        _remember(cfg, st, "simkl", str(mal_id), signature)
        note = f" rating {round(score)} (from {score})" if score is not None else ""
        pushes.append(f"{_al_title(media)} (ep{progress} {e.get('status')}{note})")

    if not (pushes or no_mal):
        log.info("  nothing to write - %d already pushed", already)
    else:
        _audit("  pushing to Simkl:", pushes)
        _audit("  no MAL id:", no_mal)
        log.info(
            "  %d pushed, %d unreachable, %d unchanged", len(pushes), len(no_mal), already
        )
    # The snapshot is updated by OUTBOUND, so nothing to persist here beyond
    # what the writes themselves recorded.
    st.save()


# --------------------------------------------------------------------------- #
# RATINGS: Floppy <-> AniList
# --------------------------------------------------------------------------- #
def plan_ratings(
    floppy_scores: dict[int, float],
    anilist_by_mal: dict[int, dict],
    winner: str = "skip",
) -> tuple[list, list, list]:
    """Decide the rating writes for one tick. Pure - no I/O, so it is testable.

    Returns (to_anilist, to_floppy, conflicts) where the first two are lists of
    (mal_id, score) and conflicts is (mal_id, floppy_score, anilist_score, winner).

    Filling a gap always happens. A disagreement means two numbers were entered
    by hand, so `winner` defaults to "skip" - report it, write nothing.
    """
    to_anilist: list[tuple[int, float]] = []
    to_floppy: list[tuple[int, float]] = []
    conflicts: list[tuple[int, float, float, str]] = []

    for mal_id, entry in anilist_by_mal.items():
        raw = entry.get("scoreRaw")
        a_score = round(int(raw) / 10.0, 1) if raw else None
        f_score = floppy_scores.get(mal_id)

        if f_score is None and a_score is None:
            continue
        if f_score is not None and a_score is not None:
            if abs(f_score - a_score) < 0.05:  # equal at 1dp
                continue
            conflicts.append((mal_id, f_score, a_score, winner))
            if winner == "floppy":
                to_anilist.append((mal_id, f_score))
            elif winner == "anilist":
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
    comparable = len(
        set(floppy_scores) | {m for m, e in anilist_by_mal.items() if e.get("scoreRaw")}
    )
    to_anilist, to_floppy, conflicts = plan_ratings(
        floppy_scores, anilist_by_mal, cfg.ratings_winner
    )
    unmatched = set(floppy_scores) - set(anilist_by_mal)

    mirror_to_mal = bool(mal and cfg.enable_mal)
    if not (to_anilist or to_floppy or conflicts or mirror_to_mal):
        log.info("RATINGS Floppy <-> AniList (no changes found)")
        return

    log.info("RATINGS Floppy(%d rated) AniList(%d rated)", len(floppy_scores), rated_al)

    if conflicts:
        log.info("  disagreements:")
        for mal_id, f_score, a_score, won_by in conflicts:
            title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
            tail = "writing neither" if won_by == "skip" else f"{won_by} wins"
            emit = log.warning if won_by == "skip" else log.info
            emit("    %s (floppy=%s anilist=%s) %s", title, f_score, a_score, tail)

    if unmatched:
        log.info("  %d rated in Floppy with no AniList entry", len(unmatched))

    # No dedup state here, deliberately. Both libraries are read in full every
    # tick, so the comparison IS the dedup: once a write lands the two sides
    # agree and the next tick finds nothing. A state file could only disagree
    # with reality - and did, masking a pending write because an earlier dry run
    # had recorded it as done.
    wrote = 0
    to_al_lines: list[str] = []
    to_fl_lines: list[str] = []
    for mal_id, score in to_anilist:
        media = anilist_by_mal[mal_id].get("media") or {}
        media_id = media.get("id")
        if not media_id:
            continue
        anilist.save(int(media_id), score_1dp=score)
        wrote += 1
        to_al_lines.append(f"{_al_title(media)} (rating -> {score} on AniList)")

    for mal_id, score in to_floppy:
        floppy.set_score(mal_id, score)
        wrote += 1
        title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
        to_fl_lines.append(f"{title} (rating -> {score} on Floppy)")

    # MAL cannot be compared - it has no list read here - so it is mirrored:
    # every rating either side agreed on is pushed, rounded.
    to_mal_lines: list[str] = []
    if mal and cfg.enable_mal:
        settled = dict(floppy_scores)
        settled.update({m: s for m, s in to_floppy})
        settled.update({m: s for m, s in to_anilist})
        for mal_id, score in sorted(settled.items()):
            entry = anilist_by_mal.get(mal_id)
            title = _al_title((entry or {}).get("media") or {}) if entry else f"mal:{mal_id}"
            mal.update(mal_id, score_1dp=score)
            to_mal_lines.append(f"{title} (rating -> {round(score)} on MAL, from {score})")

    unresolved = sum(1 for c in conflicts if c[3] == "skip")
    if to_al_lines or to_fl_lines or to_mal_lines:
        _audit("  updating:", to_al_lines + to_fl_lines)
        _audit("  mirrored to MAL:", to_mal_lines)
    elif unresolved:
        log.info("  no writes - %d disagreement(s) need a decision", unresolved)
    else:
        log.info("  no writes - both sides already agree")
    log.info("  %d written, %d unresolved", wrote, unresolved)
    st.save()


# --------------------------------------------------------------------------- #
def _loop(name: str, fn, interval: int) -> None:
    while not _stop.is_set():
        started = time.monotonic()
        try:
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
            name="outbound",
            daemon=True,
            args=(
                "outbound",
                lambda: outbound_tick(cfg, st, simkl, anilist, mal),
                cfg.poll_out_seconds,
            ),
        ),
        threading.Thread(
            target=_loop,
            name="inbound",
            daemon=True,
            args=(
                "inbound",
                lambda: inbound_tick(cfg, st, simkl, anilist),
                cfg.poll_in_seconds,
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
                    cfg.poll_ratings_seconds,
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
