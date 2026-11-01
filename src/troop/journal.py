"""Write-ahead journal: snapshot before every change, replay to restore.

The runner writes a journal entry *before* it makes each change, then flushes
to disk. If a run crashes mid-experiment, `restore()` reads the journal and
puts every object back — even across process restarts.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

from .client import DynatraceClient
from .models import Change
from .resources import undo_change


class Journal:
    def __init__(self, path: str) -> None:
        self.path = path
        self._entries: list[dict[str, Any]] = []
        if os.path.exists(path):
            self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self._entries.append(json.loads(line))

    def record(self, change: Change) -> None:
        """Append and flush before the change is applied — write-ahead."""
        entry = {
            "target": asdict(change.target),
            "before": change.before,
            "after": change.after,
            "note": change.note,
            "undone": False,
        }
        self._entries.append(entry)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def restore(self, client: DynatraceClient) -> list[str]:
        """Undo every not-yet-undone change, newest first. Idempotent-ish."""
        done: list[str] = []
        for entry in reversed(self._entries):
            if entry.get("undone"):
                continue
            done.append(undo_change(client, entry))   # dispatches on target kind
            entry["undone"] = True
        self._rewrite()
        return done

    def _rewrite(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            for entry in self._entries:
                fh.write(json.dumps(entry) + "\n")

    def pending(self) -> int:
        return sum(1 for e in self._entries if not e.get("undone"))
