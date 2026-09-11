# aniprogress

Keeps anime **progress, watchlist status and ratings** in sync between **Simkl**, **AniList**,
**MyAnimeList** and **Floppy**.

## What it does

One **reconcile** pass syncs Simkl and AniList in both directions. It reads each side once
and compares the **two live libraries** — there is no stored record of "what we wrote"
(that cache drifted from reality and caused every past incident). The comparison itself
is the dedup: once a write lands the sides agree and the next pass is a no-op.

- **Progress** is monotonic. Episodes watched do not un-happen, so the higher side wins
  and the lower side is pulled up — with an *explicit episode list*, never a bare id
  (which Simkl reads as "mark the whole show watched").
- **Watchlist status** moves both ways. A status that rides in with a progress advance is
  trusted. For a bare status disagreement at equal progress, Simkl's incremental feed
  says which side just changed it — that side wins. (A rare true simultaneous edit
  resolves toward Simkl and can be redone.)
- **`COMPLETED` is a floor.** A plan-to-watch or half-watched row on Simkl over a
  completed AniList entry is a deliberate "I'll rewatch this" marker — AniList is left
  alone. A real completion still arrives as progress.
- A title present on one side and missing on the other is **created** on the other, at
  whatever state it holds.

It does not write progress into Floppy. Floppy has its own scheduled Simkl import.

**Ratings**

A rating only ever crosses into a side that has **none** for that title — an existing
score is never overwritten.

- **Simkl ↔ AniList**: the reconcile fills whichever side is empty. Simkl stores whole
  numbers, so a rating sent there is rounded; AniList keeps the decimal.
- **Floppy ↔ AniList**: a separate ratings pass, decimals preserved both ways. When the
  two hold *different* numbers it checks the previous cycle — if exactly one side moved,
  that is the newer edit and it wins; if both moved or neither, it logs a `DECIDE` line
  and writes nothing. Set `RATINGS_WINNER=anilist` to make AniList's decimal
  authoritative instead.

Simkl already feeds Floppy on its own schedule, so a Simkl-only rating reaches Floppy
that way — the service never writes it there directly.

Both passes are a no-op when nothing has changed, so they are cheap to run continuously.

## Quick start

Create a `.env` from [`.env.example`](.env.example), then:

```yaml
services:
  aniprogress:
    image: ghcr.io/deigue/aniprogress:latest
    container_name: aniprogress
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: America/Toronto

      ENABLE_ANILIST: "true"
      ENABLE_SIMKL_PUSH: "true"
      ENABLE_MAL: "false"

      # Leave this on until you have read a cycle of logs.
      DRY_RUN: "true"
      LOG_LEVEL: "INFO"
    volumes:
      - ./aniprogress-data:/data
```

```
docker compose up -d && docker compose logs -f
```

Read the log for a full cycle, then set `DRY_RUN` to `false` and restart.

## Configuration

### Credentials

| Variable                          | Required                          | Where to get it                                                     |
| --------------------------------- | --------------------------------- | ------------------------------------------------------------------- |
| `SIMKL_CLIENT_ID`                 | always                            | [Simkl developer settings](https://simkl.com/settings/developer)    |
| `SIMKL_TOKEN`                     | always                            | OAuth2 access token for your own account                            |
| `ANILIST_TOKEN`                   | when `ENABLE_ANILIST=true`        | [AniList developer settings](https://anilist.co/settings/developer) |
| `MAL_CLIENT_ID`                   | when `ENABLE_MAL=true`            | MyAnimeList → Account Settings → API                                |
| `MAL_TOKEN` / `MAL_REFRESH_TOKEN` | when `ENABLE_MAL=true`            | `python -m aniprogress.mal_auth --client-id YOUR_ID`                |
| `FLOPPY_URL` / `FLOPPY_TOKEN`     | when `ENABLE_FLOPPY_RATINGS=true` | Your Floppy base URL, and Floppy → Settings → API                   |

### Switches

| Variable                | Default | What it does                                                                                                                             |
| ----------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `ENABLE_ANILIST`        | `true`  | Write progress, status and ratings to AniList.                                                                                           |
| `ENABLE_MAL`            | `false` | Also mirror to MyAnimeList. Ratings are rounded.                                                                                         |
| `ENABLE_SIMKL_PUSH`     | `true`  | Allow writes back to Simkl. Turn off to make Simkl read-only.                                                                            |
| `ENABLE_FLOPPY_RATINGS` | `false` | Sync ratings both ways with AniList, decimals intact. Needs `ENABLE_ANILIST`.                                                            |
| `RATINGS_WINNER`        | `skip`  | What to do when Floppy and AniList hold *different* ratings. `skip` reports it and writes neither. `floppy` or `anilist` picks a winner. |
| `DRY_RUN`               | `true`  | Log every write without sending it. **Start here.**                                                                                      |

### Cadence

| Variable             | Default | What it does                                              |
| -------------------- | ------- | -------------------------------------------------------- |
| `RECONCILE_SECONDS`  | `60`    | How often the Simkl ↔ AniList reconcile runs.            |
| `RATINGS_SECONDS`    | `900`   | How often the Floppy ↔ AniList ratings pass runs.        |

### Other

| Variable      | Default           | What it does                                                    |
| ------------- | ----------------- | ------------------------------------------------------------- |
| `STATE_DIR`   | `/data`           | Where `state.json` lives — mount it, or every restart rebuilds the Simkl snapshot from scratch. |
| `SIMKL_EPOCH` | `2010-01-01T00:00:00Z` | `date_from` for the first Simkl read. Raise it to ignore history older than a given date. |
| `LOG_LEVEL`   | `INFO`            | `DEBUG` to see every decision.                                  |
| `TZ`          | container default | Set it. Timestamps in logs are otherwise UTC.                   |

## Before you start

**Set your AniList score format to `POINT_10_DECIMAL`** (AniList → Settings → Lists). Without it,
decimal ratings render wrong. The service logs a warning at startup if it is set to anything else.

**Mount `/data`.** The Simkl snapshot lives in `state.json`; without it every restart
rebuilds it with one full Simkl read.

## Checking it without touching the network

All three stub every API client — no tokens, no requests:

- `watchlist_test.py` — the reconcile core: progress, status arbitration, the `COMPLETED`
  floor, one-sided titles.
- `ratings_test.py` — the Floppy ↔ AniList ratings pass and the Simkl rating gap-fill.
- `replay_test.py path/to/simkl-export.json` — replays a real Simkl export through the
  reconcile and verifies a second pass writes nothing.

`preflight.py` checks your credentials and settings against the live APIs before the
first real run.

## License

MIT.
