"""The canary bootstrap: setup provisions a tagged, scoped fixture set that the
monkeys can actually find; teardown removes exactly what setup created; and a
setup->plan round-trip in one process yields a non-empty plan (the thing that
was missing before — a first run no longer comes back empty)."""

import json
import os

from troop.canary import CANARY_TAG, CanaryKit, CanaryManifest, default_specs
from troop.client import DryRunClient
from troop.monkeys.fire import Baboon, Marmoset


def _kit(tmp_path, dry_run):
    client = DryRunClient()
    kit = CanaryKit(client, manifest_path=str(tmp_path / ".troop-canary.json"), dry_run=dry_run)
    return client, kit


def test_setup_creates_zone_plus_one_object_per_spec(tmp_path):
    client, kit = _kit(tmp_path, dry_run=False)
    kit.setup()
    # the manifest records the management zone + every spec that was created
    assert kit.manifest.mz_scope is not None
    assert len(kit.manifest.created) == 1 + len(default_specs())
    # every created object carries the canary tag in its value and sits in the zone
    for item in kit.manifest.created[1:]:
        obj = client.store[item["object_id"]]
        assert obj["scope"] == kit.manifest.mz_scope
        assert CANARY_TAG in str(obj["value"])


def test_setup_then_plan_is_non_empty(tmp_path):
    """The whole point: after setup, a monkey scoped to the canary finds work."""
    client, kit = _kit(tmp_path, dry_run=False)
    kit.setup()
    scope = kit.manifest.mz_scope
    assert Baboon().plan(client, scope).changes, "baboon should find the canary alerting profile"
    assert Marmoset().plan(client, scope).changes, "marmoset should find the canary metric event"


def test_setup_provisions_matching_detectors(tmp_path):
    """The gap this closes: the canary provisions the detectors that make a
    METRIC/LOG fault actually open a problem, not just the mutation targets."""
    from troop.driver import CANARY_METRIC
    client, kit = _kit(tmp_path, dry_run=False)
    kit.setup()
    created = {i["key"]: client.store[i["object_id"]] for i in kit.manifest.created[1:]}
    assert "metric-event" in created and "log-event" in created
    # the metric event actually watches the metric the driver pushes
    assert CANARY_METRIC in str(created["metric-event"]["value"])


def test_manifest_persists_and_teardown_is_exact(tmp_path):
    mpath = str(tmp_path / ".troop-canary.json")
    client = DryRunClient()
    CanaryKit(client, manifest_path=mpath, dry_run=False).setup()
    created_ids = {i["object_id"] for i in CanaryManifest.load(mpath).created}
    assert created_ids and all(oid in client.store for oid in created_ids)

    # a fresh kit reads the manifest and removes precisely those ids
    CanaryKit(client, manifest_path=mpath, dry_run=False).teardown()
    assert not any(oid in client.store for oid in created_ids)
    assert CanaryManifest.load(mpath).created == []     # manifest cleared


def test_dry_run_setup_writes_nothing_persistent(tmp_path):
    mpath = str(tmp_path / ".troop-canary.json")
    client, kit = _kit(tmp_path, dry_run=True)
    lines = kit.setup()
    assert any("dry-run" in ln for ln in lines)
    assert not os.path.exists(mpath)                    # no manifest written in dry-run


def test_teardown_without_manifest_is_a_noop(tmp_path):
    _, kit = _kit(tmp_path, dry_run=False)
    assert kit.teardown() == ["nothing to tear down (no canary manifest)."]


def test_resilient_setup_skips_a_failing_schema(tmp_path):
    """If the tenant rejects one object, setup records the skip and keeps going —
    a partial canary beats a hard stop."""
    class PickyClient(DryRunClient):
        def create_object(self, schema_id, scope, value):
            if schema_id == "builtin:anomaly-detection.metric-events":
                raise RuntimeError("schema not available on this tenant")
            return super().create_object(schema_id, scope, value)

    client = PickyClient()
    kit = CanaryKit(client, manifest_path=str(tmp_path / ".troop-canary.json"), dry_run=False)
    lines = kit.setup()
    assert any("[skip]" in ln and "metric event" in ln for ln in lines)
    # the zone and the other specs still made it into the manifest
    keys = {i["key"] for i in kit.manifest.created}
    assert "management-zone" in keys and "metric-event" not in keys
    assert "alerting-profile" in keys
