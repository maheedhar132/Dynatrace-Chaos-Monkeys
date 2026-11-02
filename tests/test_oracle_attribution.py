"""The hardened oracle: a lingering baseline problem must not read as SURVIVED,
a pre-existing problem must not be credited, and a merged baseline problem is
flagged on a KILLED verdict.

These guard the one error a monitoring-assurance tool must never make: telling
someone a piece of config is safe to delete when it is actually load-bearing.
"""

import os
import tempfile
import time

from troop.client import DryRunClient
from troop.journal import Journal
from troop.models import Blast, Change, Outcome, Plan, Reversibility, Target
from troop.monkeys.fire import CANARY_TAG
from troop.runner import Runner
from troop.safety import Fence, Guard

NOW = int(time.time() * 1000)
CANARY_ZONE = [{"id": "MZ-CANARY"}]


def _plan():
    before = {"value": {"chaos-canary": True, "severityRules": [{"x": 1}]}}
    after = {"value": {"chaos-canary": True, "severityRules": []}}
    ch = Change(Target("builtin:alerting.profile", "OBJ-1", "MZ-CANARY", label=CANARY_TAG),
                before=before, after=after, note="blind the canary profile")
    return Plan("baboon", "t", Reversibility.AUTO, Blast.OBJECT, [ch],
                fault="canary ERROR_EVENT", expected_signal="routed alert")


class ScriptedProblems(DryRunClient):
    """Returns a scripted problems list before vs after the mutation lands, so a
    test can model exactly which problem ids are open in each phase."""

    def __init__(self, before, after):
        super().__init__(store={"OBJ-1": {"objectId": "OBJ-1",
                                          "schemaId": "builtin:alerting.profile",
                                          "scope": "MZ-CANARY", "updateToken": "tok-1",
                                          "value": {"chaos-canary": True,
                                                    "severityRules": [{"x": 1}]}}})
        self._before, self._after = before, after
        self.mutated = False

    def update_object(self, object_id, value, update_token=None):
        super().update_object(object_id, value, update_token)
        self.store[object_id]["value"] = value
        self.mutated = True

    def recent_problems(self, since_ms):
        return list(self._after if self.mutated else self._before)


def _run(client):
    guard = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"})), dry_run=False)
    jp = os.path.join(tempfile.mkdtemp(), "j.jsonl")
    return Runner(client, guard, Journal(jp)).run(_plan())


def test_lingering_baseline_problem_is_not_counted_as_survived():
    """The SAME problem is open in both phases. Nothing new opened after the
    mutation, so this is KILLED — not SURVIVED off the stale problem."""
    p = {"problemId": "P-1", "title": "baseline", "startTime": NOW,
         "managementZones": CANARY_ZONE}
    r = _run(ScriptedProblems(before=[p], after=[p]))
    assert r.outcome is Outcome.KILLED
    # and the run warns the baseline problem is still open (a possible merge)
    assert any("still open" in m for m in r.messages)


def test_a_new_problem_after_mutation_is_survived():
    """Baseline problem stays open AND a genuinely new one opens -> detection
    held -> SURVIVED."""
    base = {"problemId": "P-1", "title": "baseline", "startTime": NOW,
            "managementZones": CANARY_ZONE}
    fresh = {"problemId": "P-2", "title": "fresh", "managementZones": CANARY_ZONE}
    r = _run(ScriptedProblems(before=[base], after=[base, fresh]))
    assert r.outcome is Outcome.SURVIVED


def test_old_problem_opened_before_injection_is_ignored():
    """A problem that opened well before we injected the fault is not our signal:
    the baseline never truly fires -> INCONCLUSIVE, nothing changed."""
    old = {"problemId": "P-OLD", "title": "unrelated", "startTime": NOW - 600_000,
           "managementZones": CANARY_ZONE}                      # opened 10 min ago
    sim = ScriptedProblems(before=[old], after=[old])
    r = _run(sim)
    assert r.outcome is Outcome.INCONCLUSIVE
    assert sim.mutated is False


def test_exact_scope_match_not_substring():
    """A problem whose text merely mentions the scope string (but carries no
    matching MZ and no chaos tag) is not credited; an exact zone id is."""
    p = {"problemId": "P-9", "title": "note about MZ-CANARYISH",
         "managementZones": [{"id": "MZ-OTHER"}]}
    assert Runner._matches(p, "MZ-CANARY", "run-xyz") is False
    p2 = {"problemId": "P-10", "title": "x", "managementZones": [{"id": "MZ-CANARY"}]}
    assert Runner._matches(p2, "MZ-CANARY", "run-xyz") is True
