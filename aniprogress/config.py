"""Environment-driven configuration. Every target is independently switchable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def load_dotenv(path: str = ".env") -> int:
    """Read .env into the environment without overriding what is already set.

    A no-op under Docker, where env_file/environment already populated it.
    """
    if not os.path.isfile(path):
        return 0
    loaded = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value
            loaded += 1
    return loaded


def _b(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Config:
    # --- sources -------------------------------------------------------------
    simkl_client_id: str = field(default_factory=lambda: os.environ.get("SIMKL_CLIENT_ID", ""))
    simkl_token: str = field(default_factory=lambda: os.environ.get("SIMKL_TOKEN", ""))

    floppy_url: str = field(default_factory=lambda: os.environ.get("FLOPPY_URL", "").rstrip("/"))
    floppy_token: str = field(default_factory=lambda: os.environ.get("FLOPPY_TOKEN", ""))

    # --- targets -------------------------------------------------------------
    anilist_token: str = field(default_factory=lambda: os.environ.get("ANILIST_TOKEN", ""))
    mal_client_id: str = field(default_factory=lambda: os.environ.get("MAL_CLIENT_ID", ""))
    mal_token: str = field(default_factory=lambda: os.environ.get("MAL_TOKEN", ""))

    enable_anilist: bool = field(default_factory=lambda: _b("ENABLE_ANILIST", "true"))
    enable_mal: bool = field(default_factory=lambda: _b("ENABLE_MAL", "false"))
    enable_simkl_push: bool = field(default_factory=lambda: _b("ENABLE_SIMKL_PUSH", "true"))
    enable_floppy_ratings: bool = field(default_factory=lambda: _b("ENABLE_FLOPPY_RATINGS", "false"))

    # Floppy and AniList disagree on a rating: "skip" logs both and writes
    # neither, "floppy"/"anilist" picks a winner.
    ratings_winner: str = field(
        default_factory=lambda: os.environ.get("RATINGS_WINNER", "skip").strip().lower())

    # --- cadence -------------------------------------------------------------
    poll_out_seconds: int = field(default_factory=lambda: _i("POLL_OUT_SECONDS", 60))
    poll_in_seconds: int = field(default_factory=lambda: _i("POLL_IN_SECONDS", 600))
    poll_ratings_seconds: int = field(default_factory=lambda: _i("POLL_RATINGS_SECONDS", 900))

    dry_run: bool = field(default_factory=lambda: _b("DRY_RUN", "true"))
    state_dir: str = field(default_factory=lambda: os.environ.get("STATE_DIR", "/data"))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO").upper())

    def validate(self) -> list[str]:
        """Return a list of problems; empty means good to run."""
        problems = []
        if not (self.simkl_client_id and self.simkl_token):
            problems.append("SIMKL_CLIENT_ID and SIMKL_TOKEN are required (Simkl is the event source)")
        if self.enable_anilist and not self.anilist_token:
            problems.append("ENABLE_ANILIST=true but ANILIST_TOKEN is unset")
        if self.enable_mal and not (self.mal_client_id and self.mal_token):
            problems.append("ENABLE_MAL=true but MAL_CLIENT_ID / MAL_TOKEN are unset")
        if self.enable_floppy_ratings:
            if not (self.floppy_url and self.floppy_token):
                problems.append("ENABLE_FLOPPY_RATINGS=true but FLOPPY_URL / FLOPPY_TOKEN are unset")
            if not self.enable_anilist:
                problems.append("ENABLE_FLOPPY_RATINGS=true needs ENABLE_ANILIST=true "
                                "(AniList is the other half of the pair)")
        if self.ratings_winner not in ("skip", "floppy", "anilist"):
            problems.append("RATINGS_WINNER must be 'skip', 'floppy' or 'anilist', "
                            f"got {self.ratings_winner!r}")
        return problems

    def summary(self) -> str:
        on = lambda b: "on" if b else "off"
        return (f"anilist={on(self.enable_anilist)} mal={on(self.enable_mal)} "
                f"simkl_push={on(self.enable_simkl_push)} "
                f"floppy_ratings={on(self.enable_floppy_ratings)}"
                f"{f'(conflicts: {self.ratings_winner})' if self.enable_floppy_ratings else ''} "
                f"dry_run={on(self.dry_run)} out={self.poll_out_seconds}s "
                f"in={self.poll_in_seconds}s ratings={self.poll_ratings_seconds}s")
