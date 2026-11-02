"""Runtime truth: fault-kind classification, rampage filtering, schema
verification, and the loud don't-automate-on-KILLED caution."""

import os
import tempfile

from troop.client import DryRunClient
from troop.coverage import classify, fault_note, gated_names, live_names, verify_schemas
from troop.journal import Journal
from troop.models import Blast, Change, Fault, Outcome, Plan, Reversibility, Target
from troop.monkeys.fire import CANARY_TAG
from troop.rampage import choose, eligible_monkeys
from troop.runner import Runner
from troop.safety import Fence, Guard


# --- classification ---------------------------------------------------------

def test_every_fire_monkey_has_a_fault_and_schemas():
    for c in classify():
        assert c.fault in (Fault.EVENT, Fault.METRIC, Fault.LOG, Fault.TRACE, Fault.NONE)
        assert c.schemas, f"{c.name} should declare at least one schema"


def test_injectable_faults_are_live_traffic_is_gated():
    live, gated = set(live_names()), set(gated_names())
    assert live and gated and live.isdisjoint(gated)
    # event/metric/log monkeys are live now (the driver injects their fault)
    assert {"baboon", "ape", "marmoset", "colobus"} <= live
    # trace/none monkeys are gated (need real traffic / an out-of-band fault)
    assert {"gibbon", "saki", "tarsier"} <= gated


def test_fault_note_only_for_gated():
    assert fault_note(Fault.EVENT, "baboon") is None
    assert fault_note(Fault.METRIC, "marmoset") is None
    assert fault_note(Fault.LOG, "colobus") is None
    assert "TRACE-gated" in fault_note(Fault.TRACE, "gibbon")
    assert "out-of-band" in fault_note(Fault.NONE, "tarsier")


# --- rampage filtering ------------------------------------------------------

def test_rampage_pool_is_live_only_by_default():
    assert set(eligible_monkeys()) == set(live_names())
    assert set(eligible_monkeys(live_only=False)) > set(eligible_monkeys())


def test_choose_live_only_never_picks_a_gated_monkey():
    gated = set(gated_names())
    for seed in range(25):
        name, _ = choose(DryRunClient(), "environment", seed=seed)
        assert name not in gated
    _, log = choose(DryRunClient(), "environment", seed=1)
    assert any("live-only" in line for line in log)


# --- schema verification ----------------------------------------------------

def test_verify_reports_present_and_na_against_dry_run():
    checks = verify_schemas(DryRunClient())
    by_monkey = {c.monkey: c for c in checks}
    assert by_monkey["baboon"].ok
    assert any(c.status == "n/a" and c.schema_id == "api-token" for c in checks)


def test_verify_flags_a_missing_schema_and_an_invalid_shape():
    class PickyTenant(DryRunClient):
        def schema_ids(self):
            return [s for s in super().schema_ids()
                    if s != "builtin:anomaly-detection.services"]

        def validate_object(self, schema_id, scope, value):
            if schema_id == "builtin:alerting.profile":
                return ["severityRules: unknown property"]
            return []

    checks = {(c.monkey, c.schema_id): c for c in verify_schemas(PickyTenant())}
    assert checks[("gibbon", "builtin:anomaly-detection.services")].status == "missing"
    assert checks[("baboon", "builtin:alerting.profile")].status == "invalid"


# --- the KILLED caution is always emitted -----------------------------------

def test_killed_run_emits_the_do_not_automate_caution():
    class Sim(DryRunClient):
        def __init__(self):
            super().__init__(store={"OBJ-1": {"objectId": "OBJ-1",
                                              "schemaId": "builtin:alerting.profile",
                                              "scope": "MZ-CANARY", "updateToken": "t",
                                              "value": {"chaos-canary": True, "x": 1}}})
            self.mutated = False

        def update_object(self, object_id, value, update_token=None):
            super().update_object(object_id, value, update_token)
            self.store[object_id]["value"] = value
            self.mutated = True

        def recent_problems(self, since_ms):
            return [] if self.mutated else [{"title": "chaos problem"}]

    ch = Change(Target("builtin:alerting.profile", "OBJ-1", "MZ-CANARY", label=CANARY_TAG),
                before={"value": {"chaos-canary": True, "x": 1}},
                after={"value": {"chaos-canary": True, "x": 2}}, note="x")
    plan = Plan("baboon", "t", Reversibility.AUTO, Blast.OBJECT, [ch],
                fault="f", expected_signal="e")
    guard = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"})), dry_run=False)
    jp = os.path.join(tempfile.mkdtemp(), "j.jsonl")
    r = Runner(Sim(), guard, Journal(jp)).run(plan)
    assert r.outcome is Outcome.KILLED
    assert any("do not" in m.lower() and "automat" in m.lower() for m in r.messages)
