"""Full backups taken before anything is removed or overwritten.

The write-ahead journal already stores the `before` state a revert needs. The
vault is a second, independent safety net aimed at the user's rule:

    when a monkey removes things, take a full backup of the config first.

So before the runner applies any change that **deletes or replaces** an existing
object, the vault fetches that object's *complete current server-side state* and
writes it to its own file. It is deliberately separate from the journal:

  * it captures the live object in full (not just the slice the plan carried),
  * it survives even if the journal is cleared or a revert is abandoned,
  * it is human-readable and restorable by hand from `troop restore-backup`.

Creates (nothing exists yet) have nothing to back up and are skipped.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from .client import DynatraceClient
from .models import Change


def _is_removal_or_overwrite(ch: Change) -> bool:
    """True when the change destroys or replaces something that already exists."""
    if ch.target.object_id is None:
        return False            # a create — nothing there yet
    if ch.after is None:
        return True             # a delete / revoke / disable-by-removal
    return True                 # an update replaces the prior value -> back it up


def _snapshot(client: DynatraceClient, ch: Change) -> Optional[dict[str, Any]]:
    """Pull the full current state of what `ch` is about to touch, live."""
    kind, oid = ch.target.kind, ch.target.object_id
    if oid is None:
        return None
    try:
        if kind == "settings":
            return client.get_object(oid)
        if kind == "token":
            # the secret is never retrievable; metadata is the most we can keep
            return next((t for t in client.list_tokens() if t.get("id") == oid), None)
        if kind == "synthetic":
            return next((m for m in client.list_monitors()
                         if m.get("entityId") == oid or m.get("id") == oid), None)
    except Exception as exc:  # a backup must never be the thing that aborts a run
        return {"backup_error": str(exc)}
    return None


class BackupVault:
    """Writes pre-change snapshots under `root/<run_id>/`."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.saved: list[str] = []

    def backup(self, client: DynatraceClient, ch: Change, run_id: str) -> Optional[str]:
        """Back up `ch`'s target if it removes/overwrites. Returns the file path."""
        if not _is_removal_or_overwrite(ch):
            return None
        snap = _snapshot(client, ch)
        # fall back to the plan's `before` if the live read gave nothing
        payload = {
            "run_id": run_id,
            "saved_at_ms": int(time.time() * 1000),
            "reason": "delete/revoke" if ch.after is None else "overwrite",
            "target": {
                "kind": ch.target.kind,
                "schema_id": ch.target.schema_id,
                "object_id": ch.target.object_id,
                "scope": ch.target.scope,
                "label": ch.target.label,
            },
            "note": ch.note,
            "live_snapshot": snap,
            "plan_before": ch.before,
        }
        run_dir = os.path.join(self.root, run_id)
        os.makedirs(run_dir, exist_ok=True)
        safe = f"{ch.target.kind}-{(ch.target.object_id or 'new')}".replace("/", "_").replace(":", "_")
        path = os.path.join(run_dir, f"{safe}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        self.saved.append(path)
        return path
