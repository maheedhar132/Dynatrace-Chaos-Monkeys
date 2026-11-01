"""End-to-end demo with a simulating client — no tenant needed.

Run:  PYTHONPATH=src python examples/demo.py

It seeds a canary alerting profile, then runs the Baboon monkey for real
against an in-memory tenant that *simulates detection*: a fault is detected only
while the profile still has severity rules. So blinding the profile should flip
detection off -> a KILLED verdict -> then the journal reverts it.
"""

from __future__ import annotations

import tempfile

from troop.client import DryRunClient
from troop.journal import Journal
from troop.monkeys.fire import CANARY_TAG, Baboon
from troop.runner import Runner
from troop.safety import Fence, Guard

SCOPE = "MZ-CANARY"


class SimTenant(DryRunClient):
    """A DryRunClient that actually mutates its in-memory store and simulates
    detection: a canary profile with non-empty severityRules 'catches' faults."""

    def update_object(self, object_id, value, update_token=None):
        super().update_object(object_id, value, update_token)
        self.store[object_id]["value"] = value

    def recent_problems(self, since_ms):
        prof = self.store.get("PROFILE-1", {}).get("value", {})
        catches = bool(prof.get("severityRules"))
        return [{"title": "chaos:baboon detected"}] if catches else []


def main() -> None:
    tenant = SimTenant(store={
        "PROFILE-1": {
            "objectId": "PROFILE-1",
            "schemaId": "builtin:alerting.profile",
            "scope": SCOPE,
            "value": {"name": f"{CANARY_TAG} profile", "severityRules": [{"severityLevel": "ERROR"}]},
        }
    })
    guard = Guard(fence=Fence(allowed_scopes=frozenset({SCOPE})), dry_run=False)
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        journal = Journal(tf.name)

    plan = Baboon().plan(tenant, SCOPE)
    result = Runner(tenant, guard, journal).run(plan, ttl_seconds=60)

    print("\n".join(result.messages))
    print(f"\nverdict: {result.outcome}")
    print(f"revert verified: {result.revert_verified}")
    print(f"profile restored to: {tenant.store['PROFILE-1']['value'].get('severityRules')}")


if __name__ == "__main__":
    main()
