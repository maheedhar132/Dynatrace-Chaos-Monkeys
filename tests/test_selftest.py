"""troop selftest: the end-to-end smoke that proves a live fault opens a problem."""

from troop.client import DryRunClient
from troop.coverage import live_names
from troop.models import Fault
from troop.selftest import run_selftest


def test_selftest_passes_when_detectors_fire():
    """A tenant whose detectors fire for every injected fault -> all live monkeys OK."""
    class Firing(DryRunClient):
        def recent_problems(self, since_ms):
            # a matching problem is open for whatever we just injected
            return [{"problemId": "P", "title": "chaos canary problem",
                     "managementZones": [{"id": "MZ-CANARY"}]}]

    checks = run_selftest(Firing(), "MZ-CANARY", oracle_wait=0.0)
    assert checks and all(c.fired for c in checks)
    assert {c.monkey for c in checks} == set(live_names())   # only live monkeys are probed


def test_selftest_fails_when_a_detector_is_silent():
    """A tenant that opens no problem -> every live monkey FAILs (the signal to tune)."""
    checks = run_selftest(DryRunClient(), "MZ-CANARY", oracle_wait=0.0)
    assert checks and not any(c.fired for c in checks)
    assert all("tune the detector" in c.detail for c in checks)


def test_selftest_bursts_metric_and_log_faults():
    """Metric/log faults are injected as a burst (so the detector window has data);
    event faults are a single shot."""
    class Counting(DryRunClient):
        def recent_problems(self, since_ms):
            return []   # never fires; we only care about how many injects happened

    c = Counting()
    run_selftest(c, "MZ-CANARY", oracle_wait=0.0)
    metrics = sum(1 for w in c.writes if "INGEST metric" in w)
    logs = sum(1 for w in c.writes if "INGEST log" in w)
    events = sum(1 for w in c.writes if "INGEST event" in w)
    # one metric monkey (marmoset) and one log monkey (colobus), bursted x3 each
    assert metrics == 3 and logs == 3
    # five event monkeys, one shot each
    assert events == 5


def test_selftest_credits_only_its_own_run():
    """A stale, unrelated problem (opened long before, different scope) is not
    counted as a detector firing."""
    import time

    class Stale(DryRunClient):
        def recent_problems(self, since_ms):
            return [{"problemId": "OLD", "title": "unrelated",
                     "startTime": int(time.time() * 1000) - 3_600_000,
                     "managementZones": [{"id": "MZ-OTHER"}]}]

    checks = run_selftest(Stale(), "MZ-CANARY", oracle_wait=0.0)
    assert not any(c.fired for c in checks)
