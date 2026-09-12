"""Floppy client — anime ratings only.

Floppy keys anime by MAL id, the same id AniList exposes as `idMal`, so the two
join directly. Both hold 1dp, so a rating moves either way unrounded.

Standard library only.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("aniprogress.floppy")

PAGE_SIZE = 200
MAX_PAGES = 200


def score_1dp(raw: Any) -> float | None:
    """Score to 1dp, or None when unrated. Floppy stores 0 for 'no rating'."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if v > 0 else None


class FloppyWriteUncertain(RuntimeError):
    """A write timed out or 5xx'd, so whether it landed is unknown.

    Measured: a score PATCH read-timed-out twice and then 500'd, yet the value
    HAD been applied. Reporting that as a plain failure is worse than useless -
    the log then omits a write that really happened. The next tick re-reads both
    libraries and compares, so an uncertain write self-heals either way.
    """


class Floppy:
    def __init__(self, url: str, token: str, dry_run: bool = True, timeout: float = 60.0):
        self.base = f"{url.rstrip('/')}/api/v1"
        self.token = token
        self.dry_run = dry_run
        self.timeout = timeout

    # --- transport -----------------------------------------------------------
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> Any:
        url = f"{self.base}/{path.strip('/')}/"
        if params:
            url = f"{url}?{urlencode(params)}"
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "aniprogress/1.0",
        })
        for attempt in range(3):
            try:
                with urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                return json.loads(raw.decode()) if raw else {}
            except HTTPError as e:
                # A 4xx is a real answer - do not retry it into a rate limit.
                if 400 <= e.code < 500:
                    log.error("floppy HTTP %s on %s %s: %s", e.code, method, path,
                              e.read()[:300])
                    raise
                log.debug("floppy HTTP %s on %s, retry %d", e.code, path, attempt + 1)
            except (URLError, TimeoutError) as e:
                log.debug("floppy transport error (%s), retry %d", e, attempt + 1)
            time.sleep(2 * (attempt + 1))
        log.warning("floppy %s %s failed after every retry", method, path)
        raise FloppyWriteUncertain(
            f"{method} {path} timed out or errored on every attempt" +
            (" - it may still have been applied" if method != "GET" else ""))

    # --- reads ---------------------------------------------------------------
    def _paged(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Walk a list endpoint.

        Envelope is {"pagination": {...,"next"}, "results": [...]} - `total`,
        not `count`, and a missing `next` ends the walk.
        """
        offset = 0
        for _ in range(MAX_PAGES):
            page = self._request("GET", path,
                                 params={**(params or {}), "limit": PAGE_SIZE,
                                         "offset": offset})
            rows = page.get("results") if isinstance(page, dict) else page
            if not isinstance(rows, list) or not rows:
                return
            for row in rows:
                if isinstance(row, dict):
                    yield row
            pagination = page.get("pagination") if isinstance(page, dict) else None
            if isinstance(pagination, dict) and not pagination.get("next"):
                return
            offset += len(rows)

    @staticmethod
    def _identity(row: dict) -> tuple[str, str] | None:
        """(source, media_id) for a list row.

        Both live on the nested `item`, not the row. `item_id` ("anime/mal/10020")
        is the fallback.
        """
        item = row.get("item")
        if isinstance(item, dict):
            source = str(item.get("source") or "").strip().lower()
            media_id = str(item.get("media_id") or "").strip()
            if source and media_id:
                return source, media_id
        parts = str(row.get("item_id") or "").strip().split("/")
        if len(parts) >= 3:
            return parts[1].lower(), parts[2]
        return None

    def anime_scores(self) -> dict[int, float]:
        """MAL id -> score for every rated anime. Unrated rows are omitted."""
        out: dict[int, float] = {}
        rows = 0
        skipped_source = 0
        for row in self._paged("media/anime"):
            rows += 1
            identity = self._identity(row)
            if identity is None:
                continue
            source, media_id = identity
            if source != "mal":
                skipped_source += 1
                continue
            try:
                mal_id = int(media_id)
            except (TypeError, ValueError):
                continue
            score = score_1dp(row.get("score"))
            if score is not None:
                out[mal_id] = score
        log.debug("floppy: %d anime rows read, %d rated, %d skipped (source not mal)",
                  rows, len(out), skipped_source)
        if rows and not out:
            log.warning("floppy returned %d anime rows but none carried a score - "
                        "check the token and the response shape before trusting "
                        "this as 'nothing is rated'", rows)
        return out

    # --- writes --------------------------------------------------------------
    def set_score(self, mal_id: int, score_1dp_value: float) -> dict:
        """Write a 1dp score. The anime endpoint accepts score/progress/status."""
        payload = {"score": round(float(score_1dp_value), 1)}
        if self.dry_run:
            # Summarised one line per title by the caller's audit block.
            log.debug("[dry-run] floppy PATCH media/anime/mal/%s %s", mal_id, payload)
            return {"dry_run": True}
        return self._request("PATCH", f"media/anime/mal/{int(mal_id)}", body=payload) or {}
