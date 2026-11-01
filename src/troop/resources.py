"""How each resource kind is applied and undone.

The journal persists changes as plain JSON, so an undo cannot be a closure — it
must be data the restore step can replay after a crash. This module is the one
place that maps a `Change` (plus its `Target.kind`) to the right client calls,
for both directions. Adding a new resource family means adding a branch here and
the matching client methods — nothing in the runner or journal changes.
"""

from __future__ import annotations

from typing import Any, Optional

from .client import DynatraceClient
from .models import Change, Target


def _fresh_update_token(client: DynatraceClient, object_id: str) -> Optional[str]:
    """Re-read the object to get its current updateToken (optimistic concurrency)."""
    try:
        return client.get_object(object_id).get("updateToken")
    except Exception:
        return None


def apply_change(client: DynatraceClient, ch: Change) -> Optional[str]:
    """Apply `ch`. Returns a new object/token id when the change created one."""
    k = ch.target.kind
    if k == "settings":
        if ch.before is None and ch.after is not None:
            return client.create_object(ch.target.schema_id, ch.target.scope,
                                         ch.after.get("value", ch.after))
        if ch.after is None and ch.target.object_id:
            client.delete_object(ch.target.object_id)
            return None
        if ch.target.object_id:
            client.update_object(ch.target.object_id, ch.after.get("value", ch.after),
                                 update_token=_fresh_update_token(client, ch.target.object_id))
            return None
    elif k == "token":
        if ch.after is None and ch.target.object_id:          # revoke
            client.delete_token(ch.target.object_id)
            return None
        if ch.before is None and ch.after is not None:        # create
            return client.create_token(ch.after["name"], ch.after.get("scopes", []))
    elif k == "synthetic":
        if ch.target.object_id:                               # toggle / update
            client.set_monitor(ch.target.object_id, ch.after.get("value", ch.after))
            return None
    raise ValueError(f"cannot apply change kind={k!r} ({ch.target})")


def undo_change(client: DynatraceClient, entry: dict[str, Any]) -> str:
    """Reverse one journal entry. Returns a human line for the audit log."""
    t = Target(**entry["target"])
    before, after = entry.get("before"), entry.get("after")
    k = t.kind
    if k == "settings":
        if before is None and after is not None and t.object_id:
            client.delete_object(t.object_id)
            return f"deleted created {t}"
        if before is not None and t.object_id:
            client.update_object(t.object_id, before.get("value", before),
                                 update_token=_fresh_update_token(client, t.object_id))
            return f"restored {t}"
    elif k == "token":
        if before is None and after is not None and t.object_id:
            client.delete_token(t.object_id)
            return f"revoked created token {t}"
        if before is not None:
            # a canary token: recreating yields a new secret/id, which is fine
            client.create_token(before["name"], before.get("scopes", []))
            return f"recreated token {t} (new secret)"
    elif k == "synthetic":
        if before is not None and t.object_id:
            client.set_monitor(t.object_id, before.get("value", before))
            return f"restored monitor {t}"
    return f"no-op {t}"
