#!/usr/bin/env python3
"""Read-only credential check. Makes NO writes to any service.

Requests made:
  AniList  POST https://graphql.anilist.co   query { Viewer { ... } }
  MAL      GET  https://api.myanimelist.net/v2/users/@me
  MAL      GET  https://api.myanimelist.net/v2/anime/{id}?fields=my_list_status
  Simkl    GET  https://api.simkl.com/sync/activities        (skipped if unset)

Every one is a GET or a GraphQL read. Nothing is created, updated or deleted.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROBE_MAL_ID = 8676        # Amagami SS - already completed at 7/10 in Simkl


def load_env(path=".env") -> None:
    p = pathlib.Path(path)
    if not p.exists():
        sys.exit(f"{path} not found")
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def anilist_check(token: str) -> None:
    print("\n--- AniList (read-only) ---")
    q = "query { Viewer { id name mediaListOptions { scoreFormat } } }"
    req = Request("https://graphql.anilist.co",
                  data=json.dumps({"query": q}).encode(),
                  headers={"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json",
                           "Accept": "application/json",
                           # AniList sits behind Cloudflare, which returns
                           # error 1010 to requests with no User-Agent.
                           "User-Agent": "aniprogress/1.0"})
    try:
        with urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
    except HTTPError as e:
        print(f"  FAIL  HTTP {e.code}: {e.read()[:200]!r}")
        return
    except (URLError, TimeoutError) as e:
        print(f"  FAIL  {e}")
        return
    if d.get("errors"):
        print("  FAIL ", d["errors"])
        return
    v = (d.get("data") or {}).get("Viewer") or {}
    fmt = (v.get("mediaListOptions") or {}).get("scoreFormat")
    print(f"  OK    user={v.get('name')} id={v.get('id')}")
    print(f"        scoreFormat={fmt}"
          + ("   <- correct, 1dp will render" if fmt == "POINT_10_DECIMAL"
             else f"   <- WRONG, change to POINT_10_DECIMAL in AniList settings"))


def mal_check(client_id: str, token: str) -> None:
    print("\n--- MyAnimeList (read-only) ---")
    hdr = {"Authorization": f"Bearer {token}", "X-MAL-CLIENT-ID": client_id}
    try:
        with urlopen(Request("https://api.myanimelist.net/v2/users/@me", headers=hdr),
                     timeout=30) as r:
            me = json.loads(r.read().decode())
        print(f"  OK    user={me.get('name')} id={me.get('id')}")
    except HTTPError as e:
        print(f"  FAIL  /users/@me HTTP {e.code}: {e.read()[:200]!r}")
        return
    except (URLError, TimeoutError) as e:
        print(f"  FAIL  {e}")
        return

    url = (f"https://api.myanimelist.net/v2/anime/{PROBE_MAL_ID}"
           f"?fields=id,title,my_list_status")
    try:
        with urlopen(Request(url, headers=hdr), timeout=30) as r:
            a = json.loads(r.read().decode())
        st = a.get("my_list_status") or {}
        print(f"  OK    probe mal:{PROBE_MAL_ID} \"{a.get('title')}\"")
        print(f"        current MAL state: status={st.get('status') or '(not on your list)'} "
              f"score={st.get('score')} watched={st.get('num_episodes_watched')}")
    except HTTPError as e:
        print(f"  WARN  probe read HTTP {e.code}: {e.read()[:200]!r}")


def simkl_check(client_id: str, token: str) -> None:
    print("\n--- Simkl (read-only) ---")
    if not (client_id and token):
        print("  SKIP  SIMKL_CLIENT_ID / SIMKL_TOKEN not set in .env")
        return
    req = Request("https://api.simkl.com/sync/activities",
                  headers={"Authorization": f"Bearer {token}",
                           "simkl-api-key": client_id})
    try:
        with urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        print(f"  OK    all={d.get('all')}")
        for k in ("completed", "watching", "plantowatch", "rated_at"):
            if d.get(k):
                print(f"        {k}={d[k]}")
    except HTTPError as e:
        print(f"  FAIL  HTTP {e.code}: {e.read()[:200]!r}")
    except (URLError, TimeoutError) as e:
        print(f"  FAIL  {e}")


if __name__ == "__main__":
    load_env()
    print("PREFLIGHT - read-only. No writes will be made.")
    if os.environ.get("ANILIST_TOKEN"):
        anilist_check(os.environ["ANILIST_TOKEN"])
    if os.environ.get("MAL_TOKEN"):
        mal_check(os.environ.get("MAL_CLIENT_ID", ""), os.environ["MAL_TOKEN"])
    simkl_check(os.environ.get("SIMKL_CLIENT_ID", ""), os.environ.get("SIMKL_TOKEN", ""))
    print("\ndone - nothing was modified")
