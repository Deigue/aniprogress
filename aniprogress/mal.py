"""MyAnimeList API v2 client.

MAL mirrors AniList. It is READ as well as written: without a list read there is
nothing to compare against, and the only alternatives are blind writes on every
tick or a remembered-writes cache - the drifting state that caused every earlier
incident here. `list_entries()` is that read.

MAL scores are integers 1-10, so a 1dp score is rounded on the way out. Nothing
reads a rating back from MAL to overwrite AniList's decimal; the read is only
ever used to decide whether a write is needed.

Auth: MAL uses OAuth2 with PKCE. Get a Client ID at
MyAnimeList -> Account Settings -> API -> Create ID, then run
`python -m aniprogress.mal_auth` once to mint a refresh token.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("aniprogress.mal")
BASE = "https://api.myanimelist.net/v2"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"

STATUS_MAP = {
    "CURRENT": "watching",
    "REPEATING": "watching",
    "COMPLETED": "completed",
    "PLANNING": "plan_to_watch",
    "PAUSED": "on_hold",
    "DROPPED": "dropped",
}

PAGE = 1000
MAX_PAGES = 40


class MalWriteFailed(RuntimeError):
    """A MAL write did not land. Raised so the caller can count it rather than
    reporting a write that never happened."""


class Mal:
    def __init__(self, client_id: str, token: str, refresh_token: str = "",
                 dry_run: bool = True):
        self.client_id = client_id
        self.token = token
        self.refresh_token = refresh_token
        self.dry_run = dry_run

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "X-MAL-CLIENT-ID": self.client_id,
                "User-Agent": "anime-aniprogress/1.0"}

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }).encode()
        try:
            req = Request(TOKEN_URL, data=body, headers={
                "Content-Type": "application/x-www-form-urlencoded"})
            with urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
            self.token = d.get("access_token", self.token)
            self.refresh_token = d.get("refresh_token", self.refresh_token)
            log.info("mal access token refreshed")
            return True
        except (HTTPError, URLError, TimeoutError, ValueError) as e:
            log.error("mal token refresh failed: %s", e)
            return False

    def list_entries(self) -> dict[int, dict]:
        """The whole anime list: {mal_id: {status, progress, score, total}}.

        `score` is 0 when unrated and `progress` can sit at 0 on a completed
        entry, so both are reported as MAL states them - the caller decides what
        a difference means.
        """
        out: dict[int, dict] = {}
        offset = 0
        for _ in range(MAX_PAGES):
            url = f"{BASE}/users/@me/animelist?" + urllib.parse.urlencode({
                "fields": "list_status,num_episodes", "limit": PAGE,
                "offset": offset, "nsfw": "true"})
            data = self._get(url)
            rows = (data or {}).get("data") or []
            for row in rows:
                node, ls = row.get("node") or {}, row.get("list_status") or {}
                try:
                    mid = int(node.get("id"))
                except (TypeError, ValueError):
                    continue
                out[mid] = {
                    "status": str(ls.get("status") or ""),
                    "progress": int(ls.get("num_episodes_watched") or 0),
                    "score": int(ls.get("score") or 0),
                    "total": int(node.get("num_episodes") or 0),
                }
            if not ((data or {}).get("paging") or {}).get("next"):
                break
            offset += PAGE
        log.debug("mal: %d entries", len(out))
        return out

    def _get(self, url: str) -> dict:
        for attempt in range(3):
            try:
                req = Request(url, headers=self._headers())
                with urlopen(req, timeout=45) as r:
                    return json.loads(r.read().decode())
            except HTTPError as e:
                if e.code == 401 and attempt == 0 and self._refresh():
                    continue
                if e.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                log.debug("mal GET -> HTTP %s", e.code)
                raise
            except (URLError, TimeoutError) as e:
                log.debug("mal transport error (%s), retry %d", e, attempt + 1)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError("mal list read failed after retries")

    def update(self, mal_id: int, status: str | None = None,
               progress: int | None = None, score_1dp: float | None = None) -> dict:
        """PATCH /anime/{id}/my_list_status."""
        fields: dict[str, str] = {}
        if status:
            fields["status"] = STATUS_MAP.get(status, status.lower())
        if progress is not None:
            fields["num_watched_episodes"] = str(int(progress))
        if score_1dp is not None:
            fields["score"] = str(max(1, min(10, round(float(score_1dp)))))
        if not fields:
            return {}
        if self.dry_run:
            # Summarised one line per title by the caller's audit block.
            log.debug("[dry-run] mal update %s -> %s", mal_id, fields)
            return {"dry_run": True}

        url = f"{BASE}/anime/{int(mal_id)}/my_list_status"
        data = urllib.parse.urlencode(fields).encode()
        for attempt in range(3):
            try:
                req = Request(url, data=data, method="PATCH", headers={
                    **self._headers(),
                    "Content-Type": "application/x-www-form-urlencoded"})
                with urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode())
            except HTTPError as e:
                if e.code == 401 and attempt == 0 and self._refresh():
                    continue
                if e.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                # urllib only auto-follows a redirect for GET/HEAD, so a PATCH
                # that MAL redirects (it does, for some ids) surfaced as an error
                # and the write was silently lost. Follow it by hand, once.
                if e.code in (301, 302, 307, 308):
                    target = e.headers.get("Location")
                    if target and target != url:
                        log.debug("mal PATCH %s redirected -> %s", mal_id, target)
                        url = urllib.parse.urljoin(url, target)
                        continue
                # The caller counts this and reports it in the tick summary, so
                # one short line here - not an HTML error page in the log.
                log.debug("mal PATCH %s -> HTTP %s", mal_id, e.code)
                raise MalWriteFailed(f"HTTP {e.code}") from e
            except (URLError, TimeoutError) as e:
                log.debug("mal transport error (%s), retry %d", e, attempt + 1)
                time.sleep(2 * (attempt + 1))
        raise MalWriteFailed("no response after retries")
