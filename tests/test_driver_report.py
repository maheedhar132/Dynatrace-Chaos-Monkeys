"""The fault driver (event/metric/log injection) and the mutation-score report."""

import os
import tempfile
import time

from troop.client import DryRunClient
from troop.driver import FaultDriver, drive
from troop.journal import Journal
from troop.models import Blast, Change, Fault, Outcome, Plan, Reversibility, Target
from troop.monkeys.fire import CANARY_TAG
from troop.report import (ResultStore, filter_since, parse_duration, result_row,
                          summarize, to_markdown, trend)
from troop.runner import Runner
from troop.safety import Fence, Guard


# --- driver -----------------------------------------------------------------

def test_driver_injects_each_injectable_kind():
    c = DryRunClient()
    d = FaultDriver(c)
    assert d.inject(Fault.EVENT, "m", "MZ", "r", "p") is True
    assert d.inject(Fault.METRIC, "m", "MZ", "r", "p") is True
    assert d.inject(Fault.LOG, "m", "MZ", "r", "p") is True
    assert any("INGEST event" in w for w in c.writes)
    assert any("INGEST metric" in w for w in c.writes)
    assert any("INGEST log" in w for w in c.writes)


def test_driver_cannot_inject_trace_or_none():
    d = FaultDriver(DryRunClient())
    assert d.can_inject(Fault.TRACE) is False
    assert d.inject(Fault.TRACE, "m", "MZ", "r") is False
    assert d.inject(Fault.NONE, "m", "MZ", "r") is False


def test_drive_helper_reports_trace_needs_real_traffic():
    sent, log = drive(DryRunClient(), Fault.TRACE, "MZ", run_id="r")
    assert sent == 0
    assert any("cannot synthesise" in line for line in log)


def test_drive_helper_injects_a_burst():
    c = DryRunClient()
    sent, log = drive(c, Fault.METRIC, "MZ", run_id="r", count=5)
    assert sent == 5
    assert sum(1 for w in c.writes if "INGEST metric" in w) == 5


def test_metric_monkey_baseline_is_driven_by_a_metric():
    """A METRIC-fault monkey: the driver pushes a metric, and a sim that opens a
    problem once a metric arrived produces a real KILLED/SURVIVED."""
    class MetricSim(DryRunClient):
        def __init__(self):
            super().__init__(store={"OBJ": {"objectId": "OBJ", "scope": "MZ-CANARY",
                                            "schemaId": "builtin:davis.anomaly-detectors",
                                            "updateToken": "t",
                                            "value": {"chaos-canary": True, "enabled": True}}})
            self.metric_seen = False
            self.mutated = False

        def ingest_metric(self, lines):
            super().ingest_metric(lines)
            self.metric_seen = True

        def update_object(self, object_id, value, update_token=None):
            super().update_object(object_id, value, update_token)
            self.store[object_id]["value"] = value
            self.mutated = True

        def recent_problems(self, since_ms):
            # detects only if a metric arrived AND the detector is still enabled
            if self.metric_seen and not self.mutated:
                return [{"title": "chaos metric problem"}]
            return []

    ch = Change(Target("builtin:davis.anomaly-detectors", "OBJ", "MZ-CANARY", label=CANARY_TAG),
                before={"value": {"chaos-canary": True, "enabled": True}},
                after={"value": {"chaos-canary": True, "enabled": False}}, note="disable")
    plan = Plan("marmoset", "t", Reversibility.AUTO, Blast.OBJECT, [ch],
                fault="metric", expected_signal="problem")
    guard = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"})), dry_run=False)
    jp = os.path.join(tempfile.mkdtemp(), "j.jsonl")
    r = Runner(MetricSim(), guard, Journal(jp)).run(plan, fault_kind=Fault.METRIC)
    assert r.outcome is Outcome.KILLED       # baseline fired on the metric, mutation broke it


# --- report -----------------------------------------------------------------

def _row(monkey, outcome, targets):
    r = type("R", (), {})()
    r.monkey, r.run_id, r.executed, r.revert_verified = monkey, "rid", True, True
    r.outcome = Outcome[outcome]
    plan = Plan(monkey, "t", Reversibility.AUTO, Blast.OBJECT,
                [Change(Target("s", t, "MZ"), before={}, after={}) for t in targets])
    return result_row(r, plan)


def test_store_round_trips(tmp_path):
    p = str(tmp_path / "r.jsonl")
    store = ResultStore(p)
    store.append(_row("baboon", "KILLED", ["OBJ-1"]))
    store.append(_row("ape", "SURVIVED", ["OBJ-2"]))
    assert len(ResultStore(p).rows()) == 2      # persisted and reloaded


def test_summary_scores_and_lists_dead_config():
    rows = [_row("baboon", "KILLED", ["s[OBJ-1]@MZ"]),
            _row("howler", "KILLED", ["s[OBJ-3]@MZ"]),
            _row("ape", "SURVIVED", ["s[OBJ-2]@MZ"]),
            _row("marmoset", "INCONCLUSIVE", ["s[OBJ-4]@MZ"])]
    s = summarize(rows)
    assert s["killed"] == 2 and s["survived"] == 1 and s["inconclusive"] == 1
    assert abs(s["mutation_score"] - (2 / 3)) < 1e-9     # killed / decided
    assert any("OBJ-2" in t for t in s["dead_config_candidates"])
    assert any("OBJ-1" in t or "OBJ-3" in t for t in s["load_bearing"])
    assert "marmoset" in s["inconclusive_monkeys"]


def test_markdown_renders_score_and_caution():
    md = to_markdown(summarize([_row("baboon", "SURVIVED", ["s[OBJ-1]@MZ"])]))
    assert "mutation score" in md.lower()
    assert "do not" in md.lower() and "automat" in md.lower()
    assert "OBJ-1" in md


# --- trend / since ----------------------------------------------------------

def test_parse_duration():
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration("24h") == 86400
    assert parse_duration("90m") == 5400
    assert parse_duration("30") == 30


def test_filter_since_and_trend_bucket_by_day():
    day1 = 1_700_000_000_000      # fixed epoch ms, some day
    day2 = day1 + 86_400_000      # +1 day
    rows = [
        {"ts_ms": day1, "monkey": "a", "executed": True, "outcome": "KILLED", "targets": []},
        {"ts_ms": day1, "monkey": "b", "executed": True, "outcome": "SURVIVED", "targets": []},
        {"ts_ms": day2, "monkey": "c", "executed": True, "outcome": "KILLED", "targets": []},
    ]
    assert len(filter_since(rows, day2)) == 1          # only the day-2 row
    t = trend(rows)
    assert len(t) == 2 and t[0]["mutation_score"] == 0.5 and t[1]["mutation_score"] == 1.0


# --- TraceLoad (the --drive-canary orchestration) ---------------------------

def test_trace_load_hits_the_canary_service():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from troop.driver import TraceLoad

    hits = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits["n"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with TraceLoad(url, fail=0.0, latency_ms=0, rps=50) as load:
            time.sleep(0.2)
        assert hits["n"] >= 1 and load.ok >= 1     # it actually drove the service
    finally:
        srv.shutdown()
