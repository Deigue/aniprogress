"""MyAnimeList API v2 client (mirror target only).

MAL scores are integers 1-10, so a 1dp score is rounded on the way out. That is
acceptable precisely because nothing ever reads ratings back from MAL - it is a
write-only mirror.

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
    "COMPLETED": "completed",
    "PLANNING": "plan_to_watch",
    "PAUSED": "on_hold",
    "DROPPED": "dropped",
}


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
            log.info("[dry-run] mal update %s -> %s", mal_id, fields)
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
                log.error("mal PATCH %s -> HTTP %s %s", mal_id, e.code, e.read()[:200])
                return {}
            except (URLError, TimeoutError) as e:
                log.warning("mal transport error (%s), retry %d", e, attempt + 1)
                time.sleep(2 * (attempt + 1))
        return {}
