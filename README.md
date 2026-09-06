# aniprogress

Keeps anime **progress, watchlist status and ratings** in sync between **Simkl**, **AniList**,
**MyAnimeList** and **Floppy**.

## What it does

**Progress and watchlist**

- **Simkl → AniList / MAL** — your player scrobbles to Simkl; AniList and MAL follow.
- **AniList → Simkl** — the same, back the other way, for players that write straight to AniList.

**Ratings**

| Direction        | Behaviour                                       |
| ---------------- | ----------------------------------------------- |
| AniList → Simkl  | moves ratings, **rounded** to a whole number    |
| Simkl → AniList  | **no ratings pushed**                           |
| Floppy ↔ AniList | syncs ratings **both ways, decimals preserved** |

Between Floppy and AniList, a missing rating is filled in from whichever side has one. If the two
sides hold *different* ratings, that is a person having entered two numbers — so by default neither
is overwritten and the clash is logged. Set `RATINGS_WINNER` if you would rather one side always win.

Every direction is a no-op when nothing has changed, so it is cheap to run continuously.

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

      POLL_OUT_SECONDS: "60"
      POLL_IN_SECONDS: "600"

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

| Variable               | Default | What it does                                                                |
| ---------------------- | ------- | --------------------------------------------------------------------------- |
| `POLL_OUT_SECONDS`     | `60`    | How often to check Simkl for new activity. The busy direction.              |
| `POLL_IN_SECONDS`      | `600`   | How often to check AniList. Raise it if nothing writes to AniList directly. |
| `POLL_RATINGS_SECONDS` | `900`   | How often to reconcile Floppy and AniList ratings.                          |

### Other

| Variable    | Default           | What it does                                                               |
| ----------- | ----------------- | -------------------------------------------------------------------------- |
| `STATE_DIR` | `/data`           | Where `state.json` lives. Mount it, or every restart re-checks everything. |
| `LOG_LEVEL` | `INFO`            | `DEBUG` to see every decision.                                             |
| `TZ`        | container default | Set it. Timestamps in logs are otherwise UTC.                              |

## Before you start

**Set your AniList score format to `POINT_10_DECIMAL`** (AniList → Settings → Lists). Without it,
decimal ratings render wrong. The service logs a warning at startup if it is set to anything else.

**Mount `/data`.** State lives in `state.json` and is what makes repeated runs free.

## Checking it without touching the network

`replay_test.py` replays a Simkl export through the sync logic with every API client stubbed —
no tokens, no requests. It reports what *would* be written and verifies that a second pass writes
nothing.

```
python replay_test.py path/to/simkl-export.json
```

`ratings_test.py` does the same for the Floppy ↔ AniList ratings pair — a built-in library,
no file needed:

```
python ratings_test.py
```

`preflight.py` checks your credentials and settings before the first real run.

## License

MIT.
