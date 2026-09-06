"""Durable cursors and last-written state.

Two jobs:
  * remember Simkl's activity timestamps so `date_from` stays small (Simkl
    suspends client_ids that pull full lists repeatedly)
  * remember what we last wrote to each target, so an unchanged library
    produces zero writes
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


class State:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._d: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                self._d = json.load(open(path, encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._d = {}

    # --- persistence ---------------------------------------------------------
    def save(self) -> None:
        """Atomic write: a torn state file would re-sync the whole library."""
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._d, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- generic -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._d[key] = value

    # --- write deduplication -------------------------------------------------
    def written(self, target: str, key: str) -> Any:
        return self._d.setdefault("written", {}).setdefault(target, {}).get(key)

    def mark_written(self, target: str, key: str, value: Any) -> None:
        self._d.setdefault("written", {}).setdefault(target, {})[key] = value

    def differs(self, target: str, key: str, value: Any) -> bool:
        """True when this write would actually change something."""
        return self.written(target, key) != value
