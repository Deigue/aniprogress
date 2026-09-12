"""AniList client.

Scores live in `scoreRaw` on a 0-100 scale; with POINT_10_DECIMAL set, 78 shows
as 7.8. Never write `score`, always `scoreRaw`.

Rate limit is ~90 req/min. One MediaListCollection query returns the whole list
with each entry's updatedAt, so change detection costs one request.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("aniprogress.anilist")
URL = "https://graphql.anilist.co"

LIST_QUERY = """
query ($userId: Int) {
  MediaListCollection(userId: $userId, type: ANIME) {
    lists {
      entries {
        id
        status
        progress
        scoreRaw: score(format: POINT_100)
        updatedAt
        media { id idMal episodes title { romaji english } }
      }
    }
  }
}
"""

VIEWER_QUERY = "query { Viewer { id name mediaListOptions { scoreFormat } } }"

BY_MAL_QUERY = """
query ($idMal: Int) {
  Media(idMal: $idMal, type: ANIME) { id idMal episodes title { romaji english } }
}
"""

SAVE_MUTATION = """
mutation ($mediaId: Int, $status: MediaListStatus, $progress: Int, $scoreRaw: Int) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status, progress: $progress, scoreRaw: $scoreRaw) {
    id status progress score(format: POINT_100)
  }
}
"""


class AniList:
    def __init__(self, token: str, dry_run: bool = True):
        self.token = token
        self.dry_run = dry_run
        self._viewer_id: int | None = None
        self._mal_cache: dict[int, dict] = {}
        # AniList reports the remaining budget on every response. Tracking it
        # lets us wait for the window instead of firing into a 429.
        self._remaining: int | None = None
        self._reset_at: float = 0.0

    def _note_budget(self, headers) -> None:
        try:
            self._remaining = int(headers.get("X-RateLimit-Remaining"))
        except (TypeError, ValueError):
            self._remaining = None
        try:
            self._reset_at = float(headers.get("X-RateLimit-Reset") or 0)
        except (TypeError, ValueError):
            self._reset_at = 0.0

    def _pace(self) -> None:
        """Hold off while the budget is nearly spent, rather than firing into a
        429. AniList does not always send X-RateLimit-Reset, so without one fall
        back to a slice of the minute-long window instead of a 1s busy-wait."""
        if self._remaining is None or self._remaining > 2:
            return
        wait = (self._reset_at - time.time()) if self._reset_at else 10.0
        wait = min(max(wait, 1.0), 65.0)
        log.info("anilist budget nearly spent (%s left), pausing %.0fs",
                 self._remaining, wait)
        time.sleep(wait)
        self._remaining = None

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = Request(URL, data=body, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "anime-aniprogress/1.0",
        })
        for attempt in range(4):
            self._pace()
            try:
                with urlopen(req, timeout=45) as r:
                    self._note_budget(r.headers)
                    payload = json.loads(r.read().decode())
                if payload.get("errors"):
                    log.error("anilist errors: %s", payload["errors"])
                return payload.get("data") or {}
            except HTTPError as e:
                if e.code == 429:
                    # A real AniList limit carries X-RateLimit-* / Retry-After.
                    # An nginx or Cloudflare edge 429 carries NEITHER and is
                    # transient - sleeping the 60s default on one of those cost
                    # four minutes per title and killed whole ticks, leaving the
                    # run half applied. Tell them apart.
                    retry_after = e.headers.get("Retry-After")
                    remaining = e.headers.get("X-RateLimit-Remaining")
                    if retry_after or remaining is not None:
                        self._note_budget(e.headers)
                        wait = min(int(retry_after or 60), 60)
                        log.warning("anilist rate limited (%s of %s left), "
                                    "sleeping %ss", remaining,
                                    e.headers.get("X-RateLimit-Limit"), wait)
                    else:
                        wait = 2 * (2 ** attempt)
                        log.warning("anilist edge 429 (no rate-limit headers, "
                                    "transient), retry %d in %ss",
                                    attempt + 1, wait)
                    time.sleep(wait)
                    continue
                # AniList sits behind Cloudflare and returns transient 502/520/
                # 524 under load. Those are not answers, so retry them the same
                # way a dropped connection is retried. A 4xx *is* an answer and
                # must not be retried into a rate limit.
                if e.code >= 500:
                    log.warning("anilist HTTP %s (transient), retry %d",
                                e.code, attempt + 1)
                    time.sleep(3 * (attempt + 1))
                    continue
                # 404 is a valid "no such Media" answer (by_mal lookups); the
                # caller handles it, so do not shout about it.
                lvl = log.debug if e.code == 404 else log.error
                lvl("anilist HTTP %s: %s", e.code, e.read()[:300])
                raise
            except (URLError, TimeoutError) as e:
                log.warning("anilist transport error (%s), retry %d", e, attempt + 1)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError("anilist request failed after retries")

    # --- reads ---------------------------------------------------------------
    def viewer(self) -> dict:
        return (self._gql(VIEWER_QUERY) or {}).get("Viewer") or {}

    def viewer_id(self) -> int:
        if self._viewer_id is None:
            self._viewer_id = int(self.viewer().get("id") or 0)
        return self._viewer_id

    def list_entries(self) -> list[dict]:
        """The whole anime list, one request, deduplicated.

        MediaListCollection returns an entry once per list it belongs to, so a
        title on a custom list comes back two or three times. Measured: 309 rows
        for 270 entries.
        """
        data = self._gql(LIST_QUERY, {"userId": self.viewer_id()})
        seen: dict[int, dict] = {}
        raw = 0
        for lst in ((data.get("MediaListCollection") or {}).get("lists") or []):
            for entry in (lst.get("entries") or []):
                raw += 1
                key = entry.get("id") or (entry.get("media") or {}).get("id")
                if key is None:
                    continue
                # Keep the most recently touched copy; they are normally identical.
                prev = seen.get(int(key))
                if prev is None or int(entry.get("updatedAt") or 0) >= int(prev.get("updatedAt") or 0):
                    seen[int(key)] = entry
        if raw != len(seen):
            log.debug("anilist: %d list rows collapsed to %d unique entries "
                      "(custom lists overlap the status lists)", raw, len(seen))
        return list(seen.values())

    def by_mal(self, id_mal: int) -> dict | None:
        """MAL id -> AniList media, or None. AniList 404s when it has no such
        anime; that is a real answer here, not an error - swallow it."""
        if id_mal in self._mal_cache:
            return self._mal_cache[id_mal]
        try:
            media = (self._gql(BY_MAL_QUERY, {"idMal": int(id_mal)}) or {}).get("Media")
        except HTTPError as e:
            if e.code != 404:
                raise
            media = None          # a real "no such anime" - remember it
        except (URLError, TimeoutError, RuntimeError) as e:
            # Could not ask. NOT cached: the answer is unknown, not absent, and
            # one unreachable lookup must not abort the rest of the tick.
            log.warning("anilist lookup for mal:%s unavailable (%s)", id_mal, e)
            return None
        self._mal_cache[id_mal] = media  # cache misses too, so we ask once
        return media

    # --- writes --------------------------------------------------------------
    def save(self, media_id: int, status: str | None = None,
             progress: int | None = None, score_1dp: float | None = None) -> dict:
        variables: dict[str, Any] = {"mediaId": int(media_id)}
        if status:
            variables["status"] = status
        if progress is not None:
            variables["progress"] = int(progress)
        if score_1dp is not None:
            # 7.8 -> 78 on the 0-100 raw scale; renders as 7.8 under POINT_10_DECIMAL
            variables["scoreRaw"] = int(round(float(score_1dp) * 10))
        if self.dry_run:
            # Summarised one line per title by the caller's audit block.
            log.debug("[dry-run] anilist save %s", variables)
            return {"dry_run": True}
        return (self._gql(SAVE_MUTATION, variables) or {}).get("SaveMediaListEntry") or {}


# Simkl watchlist vocabulary -> AniList MediaListStatus
STATUS_MAP = {
    "watching": "CURRENT",
    "completed": "COMPLETED",
    "plantowatch": "PLANNING",
    "plan to watch": "PLANNING",
    "hold": "PAUSED",
    "on hold": "PAUSED",
    "dropped": "DROPPED",
}
