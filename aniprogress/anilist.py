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

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = Request(URL, data=body, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "anime-aniprogress/1.0",
        })
        for attempt in range(4):
            try:
                with urlopen(req, timeout=45) as r:
                    payload = json.loads(r.read().decode())
                if payload.get("errors"):
                    log.error("anilist errors: %s", payload["errors"])
                return payload.get("data") or {}
            except HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", 60))
                    log.warning("anilist rate limited, sleeping %ss", wait)
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
                log.error("anilist HTTP %s: %s", e.code, e.read()[:300])
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
        """MAL id -> AniList media."""
        if id_mal in self._mal_cache:
            return self._mal_cache[id_mal]
        media = (self._gql(BY_MAL_QUERY, {"idMal": int(id_mal)}) or {}).get("Media")
        if media:
            self._mal_cache[id_mal] = media
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
