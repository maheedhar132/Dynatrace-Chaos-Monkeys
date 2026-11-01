"""The v0 trust additions: three-state verdict, revert-by-reread, updateToken,
timeline markers, and the doctor preflight."""

from troop.client import DryRunClient
from troop.config import parse_config
from troop.journal import Journal
from troop.models import Blast, Change, Outcome, Plan, Reversibility, Target
from troop.monkeys.fire import CANARY_TAG
from troop.preflight import run_checks, worst
from troop.runner import Runner
from troop.safety import Fence, Guard


def _plan():
    before = {"value": {"chaos-canary": True, "severityRules": [{"x": 1}]}}
    after = {"value": {"chaos-canary": True, "severityRules": []}}
    ch = Change(Target("builtin:alerting.profile", "OBJ-1", "MZ-CANARY", label=CANARY_TAG),
                before=before, after=after, note="blind the canary profile")
    return Plan("baboon", "t", Reversibility.AUTO, Blast.OBJECT, [ch],
                fault="canary ERROR_EVENT", expected_signal="routed alert")


class Sim(DryRunClient):
    """Detection depends on config health: fires `detect_before` until the
    mutation lands, then `detect_after`."""

    def __init__(self, detect_before=True, detect_after=True):
        super().__init__(store={"OBJ-1": {"objectId": "OBJ-1",
                                           "schemaId": "builtin:alerting.profile",
                                           "scope": "MZ-CANARY", "updateToken": "tok-1",
                                           "value": {"chaos-canary": True,
                                                     "severityRules": [{"x": 1}]}}})
        self.detect_before, self.detect_after = detect_before, detect_after
        self.mutated = False
        self.tokens_seen = []

    def update_object(self, object_id, value, update_token=None):
        self.tokens_seen.append(update_token)
        super().update_object(object_id, value, update_token)
        self.store[object_id]["value"] = value
        self.mutated = True

    def recent_problems(self, since_ms):
        fires = self.detect_after if self.mutated else self.detect_before
        return [{"title": "chaos problem"}] if fires else []


def _run(sim, tmp_path):
    guard = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"})), dry_run=False)
    return Runner(sim, guard, Journal(str(tmp_path / "j.jsonl"))).run(_plan())


# --- three-state verdict ----------------------------------------------------

def test_killed_when_detection_breaks(tmp_path):
    r = _run(Sim(detect_before=True, detect_after=False), tmp_path)
    assert r.outcome is Outcome.KILLED and r.killed is True


def test_survived_when_detection_holds(tmp_path):
    r = _run(Sim(detect_before=True, detect_after=True), tmp_path)
    assert r.outcome is Outcome.SURVIVED and r.killed is False


def test_inconclusive_when_baseline_never_fires(tmp_path):
    sim = Sim(detect_before=False, detect_after=False)
    r = _run(sim, tmp_path)
    assert r.outcome is Outcome.INCONCLUSIVE
    assert r.executed is False and sim.mutated is False   # nothing was changed


# --- revert verification by re-read -----------------------------------------

def test_revert_is_verified_by_reread(tmp_path):
    sim = Sim(detect_before=True, detect_after=False)
    r = _run(sim, tmp_path)
    assert r.revert_verified is True
    assert sim.store["OBJ-1"]["value"]["severityRules"] == [{"x": 1}]  # really back


# --- optimistic concurrency -------------------------------------------------

def test_update_carries_the_update_token(tmp_path):
    sim = Sim(detect_before=True, detect_after=True)
    _run(sim, tmp_path)
    assert "tok-1" in sim.tokens_seen   # fetched fresh and passed on apply + undo


# --- dogfood markers --------------------------------------------------------

def test_experiment_emits_timeline_markers(tmp_path):
    sim = Sim(detect_before=True, detect_after=False)
    _run(sim, tmp_path)
    assert any("Chaos experiment started" in w for w in sim.writes)
    assert any("Chaos experiment ended" in w for w in sim.writes)


# --- doctor / preflight -----------------------------------------------------

def test_doctor_flags_empty_fence():
    cfg = parse_config({"tenants": [{"id": "a", "canary_scopes": []}]})
    checks = run_checks(cfg, cfg.tenant("a"), DryRunClient())
    assert worst(checks) == "error"
    assert any(c.name == "fence.canary_scopes" and not c.ok for c in checks)


def test_doctor_rejects_environment_as_canary():
    cfg = parse_config({"tenants": [{"id": "a", "canary_scopes": ["environment"]}]})
    checks = run_checks(cfg, cfg.tenant("a"), DryRunClient())
    assert any(c.name == "fence.canary_scopes" and not c.ok for c in checks)


def test_doctor_passes_a_sane_dry_run_config():
    cfg = parse_config({"tenants": [{"id": "a", "canary_scopes": ["MZ-CANARY"]}]})
    checks = run_checks(cfg, cfg.tenant("a"), DryRunClient())
    assert worst(checks) != "error"
