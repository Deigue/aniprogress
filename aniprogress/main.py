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
    acts = simkl.activities()
    if not acts:
        return
    newest = acts.get("all") or ""
    if newest and newest == st.get("simkl_activity_all"):
        log.debug("simkl unchanged (%s) - no-op", newest)
        return

    date_from = st.get("simkl_anime_cursor")
    blob = simkl.all_items("anime", date_from=date_from)
    entries = Simkl.anime_entries(blob)
    log.info("OUTBOUND  to AniList/MAL")
    log.info("  Simkl titles   : %d anime changed since %s",
             len(entries), date_from or "the beginning")

    # One request: the current AniList state, so no write can regress it.
    current: dict[int, dict] = {}
    if anilist and cfg.enable_anilist:
        try:
            current = {
                int((e.get("media") or {}).get("id") or 0): e
                for e in anilist.list_entries()
            }
            log.info("  AniList titles : %d already tracked", len(current))
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
                    st.mark_written("anilist", key, signature)
                    log.debug(
                        "anilist[%s] no-op, target already equal or better | %s",
                        target,
                        (e.get("show") or {}).get("title"),
                    )
                else:
                    g_status, g_prog, why = decision
                    # No score argument: Simkl never rates AniList.
                    anilist.save(target, status=g_status, progress=g_prog)
                    st.mark_written("anilist", key, signature)
                    have = current.get(int(target)) or {}
                    bits = []
                    if g_prog is not None:
                        bits.append(f"progress ep{int(have.get('progress') or 0)} -> ep{g_prog}")
                    if g_status:
                        bits.append(f"status {have.get('status') or '-'} -> {g_status}")
                    line = f"{_title_of(e):<45.45} {', '.join(bits)}"
                    (adds if why == "new" else updates).append(line)

        if mal and cfg.enable_mal and mal_id and st.differs("mal", key, signature):
            mal.update(mal_id, status=status, progress=progress, score_1dp=score)
            st.mark_written("mal", key, signature)
            mal_writes.append(f"{_title_of(e):<45.45} ep{progress} status={status} score={score}")

    _audit("  updating:", updates)
    _audit("  adding titles:", adds)
    _audit("  also mirrored to MAL:", mal_writes)
    _audit("  no AniList entry - cannot write:", unmatched)
    log.info("  unchanged      : %d already equal or better on AniList", skipped)
    log.info("  result         : %d updated, %d added, %d unmatched",
             len(updates), len(adds), len(unmatched))
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
    newest = max(int(e.get("updatedAt") or 0) for e in entries)
    seen = int(st.get("anilist_updated_at") or 0)

    if seen == 0:
        # First run: every entry looks "changed" because there is no cursor yet.
        # Pushing all of them would fire hundreds of Simkl writes for data Simkl
        # very likely already has. Record the watermark and start reacting from
        # the next real change instead.
        st.set("anilist_updated_at", newest)
        st.save()
        log.info(
            "inbound: seeded cursor at updatedAt=%s from %d entries "
            "(no writes on first run)",
            newest,
            len(entries),
        )
        return

    if newest <= seen:
        log.debug("anilist unchanged (updatedAt=%s) - no-op", newest)
        return

    changed = [e for e in entries if int(e.get("updatedAt") or 0) > seen]
    log.info("INBOUND   to Simkl")
    log.info("  AniList titles : %d tracked, %d changed since cursor %s",
             len(entries), len(changed), seen)
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
        st.mark_written("simkl", str(mal_id), signature)
        rating_note = f", rating {round(score)} (rounded from {score})" if score is not None else ""
        pushes.append(f"{_al_title(media):<45.45} ep{progress} {e.get('status')}{rating_note}")

    _audit("  pushing to Simkl:", pushes)
    _audit("  no MAL id - cannot reach Simkl:", no_mal)
    log.info("  unchanged      : %d already pushed with the same values", already)
    log.info("  result         : %d pushed, %d unreachable", len(pushes), len(no_mal))

    st.set("anilist_updated_at", newest)
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


def ratings_tick(cfg: Config, st: State, floppy, anilist: AniList | None) -> None:
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
    comparable = len(set(floppy_scores) | {m for m, e in anilist_by_mal.items() if e.get("scoreRaw")})
    log.info("RATINGS   Floppy <-> AniList")
    log.info("  Floppy         : %d rated", len(floppy_scores))
    log.info("  AniList        : %d entries, %d with a MAL id, %d rated",
             len(entries), len(anilist_by_mal), rated_al)
    log.info("  comparable     : %d titles rated on at least one side", comparable)

    to_anilist, to_floppy, conflicts = plan_ratings(
        floppy_scores, anilist_by_mal, cfg.ratings_winner
    )

    if conflicts:
        log.info("  disagreements:")
        for mal_id, f_score, a_score, won_by in conflicts:
            title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
            if won_by == "skip":
                log.warning("    %-45.45s floppy=%s anilist=%s  WRITING NEITHER "
                            "(set RATINGS_WINNER to resolve)", title, f_score, a_score)
            else:
                log.info("    %-45.45s floppy=%s anilist=%s -> %s wins",
                         title, f_score, a_score, won_by)

    unmatched = set(floppy_scores) - set(anilist_by_mal)
    if unmatched:
        log.info("  %d rated in Floppy with no AniList entry - add them on AniList first",
                 len(unmatched))

    wrote = 0
    to_al_lines: list[str] = []
    to_fl_lines: list[str] = []
    for mal_id, score in to_anilist:
        signature = f"score={score}"
        if not st.differs("floppy_to_anilist", str(mal_id), signature):
            continue
        media = anilist_by_mal[mal_id].get("media") or {}
        media_id = media.get("id")
        if not media_id:
            continue
        anilist.save(int(media_id), score_1dp=score)
        st.mark_written("floppy_to_anilist", str(mal_id), signature)
        wrote += 1
        to_al_lines.append(f"{_al_title(media):<45.45} rating -> {score}  (AniList)")

    for mal_id, score in to_floppy:
        signature = f"score={score}"
        if not st.differs("anilist_to_floppy", str(mal_id), signature):
            continue
        floppy.set_score(mal_id, score)
        st.mark_written("anilist_to_floppy", str(mal_id), signature)
        wrote += 1
        title = _al_title((anilist_by_mal.get(mal_id) or {}).get("media") or {})
        to_fl_lines.append(f"{title:<45.45} rating -> {score}  (Floppy)")

    _audit("  updating:", to_al_lines + to_fl_lines,
           empty="nothing to write - both sides already agree")
    log.info("  result         : %d written, %d disagreements left alone",
             wrote, sum(1 for c in conflicts if c[3] == "skip"))
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
                    lambda: ratings_tick(cfg, st, floppy, anilist),
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
