"""Potto watcher demo — no tenant needed.

Run:  PYTHONPATH=src python examples/watch_demo.py

Shows both outcomes:
  * a tenant that AUDITS settings changes  -> OBSERVABLE
  * a tenant that does NOT                  -> BLIND SPOT
Both probe with an inert canary object and revert it via the journal.
"""

from __future__ import annotations

import tempfile
import time

from troop.client import DryRunClient
from troop.monkeys.watch import Potto
from troop.safety import Fence, Guard

SCOPE = "MZ-CANARY"


class AuditingTenant(DryRunClient):
    """Records a SETTINGS audit event whenever an object is created."""

    def create_object(self, schema_id, scope, value):
        oid = super().create_object(schema_id, scope, value)
        self.audit.append({"timestamp": int(time.time() * 1000), "eventType": "CREATE",
                           "category": "SETTINGS", "entityId": oid, "payload": str(value)})
        return oid


def run(tenant, label):
    guard = Guard(fence=Fence(allowed_scopes=frozenset({SCOPE})), dry_run=False)
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        from troop.journal import Journal
        journal = Journal(tf.name)
    result = Potto().run(tenant, guard, journal, SCOPE, timeout_s=0, poll_s=0)
    print(f"\n=== {label} ===")
    print("\n".join(result.messages))
    print(f"verdict: {'BLIND SPOT' if result.killed else 'OBSERVABLE'}")


if __name__ == "__main__":
    run(AuditingTenant(), "tenant that audits settings changes")
    run(DryRunClient(), "tenant with no audit coverage")
