"""Durable cursors and the Simkl snapshot.

Deliberately NOT a record of what was written. Every target is read before it is
written, so the comparison of two live libraries is the dedup; a remembered-write
cache could only disagree with reality, and each time it did it caused an
incident. What is kept here is Simkl's own data and the cursors that let it be
re-read cheaply - Simkl suspends client_ids that pull full lists repeatedly.
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
