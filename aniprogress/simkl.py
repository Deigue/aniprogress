"""Simkl client.

Simkl's docs are explicit: pulling /sync/all-items without first checking
/sync/activities, and without a date_from, gets your client_id suspended. So
every read here is activity-gated and incremental.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("aniprogress.simkl")
BASE = "https://api.simkl.com"


class Simkl:
    def __init__(self, client_id: str, token: str, dry_run: bool = True):
        self.client_id = client_id
        self.token = token
        self.dry_run = dry_run

    # --- transport -----------------------------------------------------------
    def _req(self, method: str, path: str, params: dict | None = None,
             body: Any = None) -> Any:
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "simkl-api-key": self.client_id,
            "Content-Type": "application/json",
            "User-Agent": "anime-aniprogress/1.0",
        })
        for attempt in range(4):
            try:
                with urlopen(req, timeout=45) as r:
                    raw = r.read().decode("utf-8").strip()
                return json.loads(raw) if raw else None
            except HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", 2 * (attempt + 1)))
                    log.warning("simkl 429, sleeping %ss", wait)
                    time.sleep(wait)
                    continue
                log.error("simkl %s %s -> HTTP %s", method, path, e.code)
                raise
            except (URLError, TimeoutError) as e:
                log.warning("simkl transport error (%s), retry %d", e, attempt + 1)
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"simkl {method} {path} failed after retries")

    # --- reads ---------------------------------------------------------------
    def activities(self) -> dict:
        """Cheap change probe. Always call this before all_items()."""
        return self._req("GET", "/sync/activities") or {}

    def all_items(self, media_type: str = "anime", status: str | None = None,
                  date_from: str | None = None, extended: str = "full") -> Any:
        """Incremental pull. date_from is effectively mandatory in steady state."""
        path = f"/sync/all-items/{media_type}"
        if status:
            path += f"/{status}"
        params: dict[str, str] = {"extended": extended, "episode_watched_at": "yes"}
        if date_from:
            params["date_from"] = date_from
        else:
            log.warning("all_items(%s) without date_from - full pull, use sparingly",
                        media_type)
        return self._req("GET", path, params)

    def resolve_mal(self, mal_id: int) -> int | None:
        """MAL id -> the Simkl id Simkl would actually write to, or None.

        Not "does Simkl know this id". Simkl resolves a special, short or split
        film to its PARENT series and still echoes back the mal id you asked for,
        so a bare presence check says yes and the write lands on the parent - nine
        episodes of "Sword Art OFFline" onto Sword Art Online. The caller needs
        the target id so it can tell a genuine new title from one of those.

        A transport failure is not an answer, so it is reported as unresolvable
        rather than as absent.
        """
        try:
            res = self._req("GET", "/search/id",
                            params={"mal": str(int(mal_id))})
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as e:
            log.warning("simkl lookup for mal:%s failed (%s)", mal_id, e)
            return None
        for hit in (res or []):
            if str(((hit.get("mal") or {}).get("id"))) != str(int(mal_id)):
                continue          # a fuzzy match, not this title
            sid = (hit.get("ids") or {}).get("simkl")
            try:
                return int(sid)
            except (TypeError, ValueError):
                return None
        return None

    # --- writes --------------------------------------------------------------
    def add_history(self, payload: dict) -> Any:
        """POST /sync/history - payload shaped as {"anime":[{ids:{...}, ...}]}."""
        if self.dry_run:
            # The caller already logs one readable line per title. Dumping the
            # episode array as well is what makes the container log unreadable.
            log.debug("[dry-run] simkl add_history %s", json.dumps(payload)[:400])
            return {"dry_run": True}
        return self._req("POST", "/sync/history", body=payload)

    def add_to_list(self, payload: dict) -> Any:
        """POST /sync/add-to-list - put a title on a list without watching it.

        This is the safe way to bring a plan-to-watch title across. /sync/history
        marks episodes watched; a bare id there marks the WHOLE show watched,
        which is what caused the 2026-09-07 incident.
        """
        if self.dry_run:
            log.debug("[dry-run] simkl add_to_list %s", json.dumps(payload)[:400])
            return {"dry_run": True}
        return self._req("POST", "/sync/add-to-list", body=payload)

    def remove_history(self, payload: dict) -> Any:
        """POST /sync/history/remove - same payload shape as add_history.

        A bare {"ids": {...}} with no episodes removes the whole show, which is
        the mirror image of the bug this exists to undo.
        """
        if self.dry_run:
            log.debug("[dry-run] simkl remove_history %s", json.dumps(payload)[:400])
            return {"dry_run": True}
        return self._req("POST", "/sync/history/remove", body=payload)

    def add_rating(self, payload: dict) -> Any:
        if self.dry_run:
            log.debug("[dry-run] simkl add_rating %s", json.dumps(payload)[:400])
            return {"dry_run": True}
        return self._req("POST", "/sync/ratings", body=payload)

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def anime_entries(blob: Any) -> list[dict]:
        """Normalise /sync/all-items anime output to a flat list."""
        if not blob:
            return []
        if isinstance(blob, dict):
            return list(blob.get("anime") or [])
        return list(blob)

    @staticmethod
    def ids_of(entry: dict) -> dict:
        node = entry.get("show") or entry.get("movie") or entry.get("anime") or entry
        return dict(node.get("ids") or {})
