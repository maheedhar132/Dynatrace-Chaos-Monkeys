"""The fault driver: the thing that actually *causes* the fault a monkey tests.

A monkey mutates a piece of config; the driver injects the fault that config is
supposed to catch, so the oracle can see whether it still does. Three fault
kinds can be synthesised over the Dynatrace ingest APIs, so the monkeys that use
them are live with no extra setup:

  * EVENT  -> POST /api/v2/events/ingest   (an ERROR_EVENT opens a problem)
  * METRIC -> POST /api/v2/metrics/ingest  (a canary metric crosses a detector)
  * LOG    -> POST /api/v2/logs/ingest     (a canary log line hits the pipeline)

TRACE faults (a real service erroring, latency, failure-rate, SLO burn) can't be
faked from an API — they need request traffic through an OneAgent-monitored
service. The driver is honest about that: it returns `False` for TRACE/NONE and
the runner reports INCONCLUSIVE with a pointer to the reference canary service
(`examples/canary_service.py`) or `troop drive`.

Everything the driver injects is tagged with the run id and `chaos` so the oracle
can attribute it and so it is never mistaken for a real signal.
"""

from __future__ import annotations

import threading
import urllib.request
from typing import Optional

from .client import DynatraceClient
from .models import Fault

# Fault kinds the driver can synthesise unaided (so the monkey is "live now").
INJECTABLE = frozenset({Fault.EVENT, Fault.METRIC, Fault.LOG})

CANARY_METRIC = "chaos.canary.fault"


class FaultDriver:
    """Injects the fault a monkey's baseline needs, by kind."""

    def __init__(self, client: DynatraceClient) -> None:
        self.client = client

    def can_inject(self, fault: Fault) -> bool:
        return fault in INJECTABLE

    def inject(self, fault: Fault, monkey: str, scope: str, run_id: str,
               phase: str = "probe") -> bool:
        """Inject one fault of `fault` kind. Returns True if something was sent.

        Returns False for TRACE/NONE — the caller must drive those out of band.
        """
        if fault is Fault.EVENT:
            self.client.ingest_event(self.event(monkey, scope, run_id, phase))
            return True
        if fault is Fault.METRIC:
            self.client.ingest_metric(self.metric(monkey, scope, run_id))
            return True
        if fault is Fault.LOG:
            self.client.ingest_log(self.log(monkey, scope, run_id))
            return True
        return False

    # --- payload builders (also reused by `troop drive`) --------------------

    def event(self, monkey: str, scope: str, run_id: str, phase: str = "probe") -> dict:
        return {
            "eventType": "ERROR_EVENT",
            "title": f"chaos:{monkey}:{phase}",
            "properties": {"chaos": "true", "chaos.run_id": run_id, "chaos.phase": phase,
                           "chaos.monkey": monkey, "chaos.scope": scope},
        }

    def metric(self, monkey: str, scope: str, run_id: str, value: float = 1.0) -> str:
        # Dynatrace metric line protocol: key,dim=val value
        dims = f"scope={_san(scope)},monkey={_san(monkey)},run_id={_san(run_id)},chaos=true"
        return f"{CANARY_METRIC},{dims} {value}"

    def log(self, monkey: str, scope: str, run_id: str) -> list[dict]:
        return [{
            "content": f"chaos-canary fault monkey={monkey} run_id={run_id}",
            "severity": "ERROR",
            "chaos": "true", "chaos.run_id": run_id,
            "chaos.monkey": monkey, "chaos.scope": scope,
        }]


def _san(s: str) -> str:
    """Make a string safe as a metric dimension value (no spaces/commas)."""
    return "".join(c if c.isalnum() or c in "-_.:" else "_" for c in str(s))[:120]


class TraceLoad:
    """Background load against the reference canary service, for the duration of a
    TRACE-monkey run — the orchestration behind `troop run --drive-canary`.

    A TRACE monkey can't be driven by an ingest API; it needs a real service to
    degrade. This hammers the canary's `/work` endpoint (with a failure rate and
    latency) in a background thread while the run is in progress, so the baseline
    and the post-mutation check have real traffic to detect. Context-managed:
    starts on enter, stops and joins on exit. It only generates HTTP load — it
    touches nothing in Dynatrace.
    """

    def __init__(self, url: str, fail: float = 0.5, latency_ms: int = 800,
                 rps: float = 5.0) -> None:
        self.url = f"{url.rstrip('/')}/work?fail={fail}&latency={latency_ms}"
        self.rps = max(0.1, rps)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.ok = 0
        self.err = 0

    def __enter__(self) -> "TraceLoad":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        interval = 1.0 / self.rps
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=10) as r:
                    self.ok += 1 if r.status == 200 else 0
                    self.err += 0 if r.status == 200 else 1
            except Exception:
                self.err += 1
            self._stop.wait(interval)


def drive(client: DynatraceClient, fault: Fault, scope: str, *, run_id: str,
          monkey: str = "manual", count: int = 1) -> tuple[int, list[str]]:
    """Fire `count` faults of one kind at a scope, out of band. Returns
    (sent, log_lines). Used by `troop drive` so an operator can generate real
    errors/metrics/logs on demand against the canary — the API-side half of a
    canary workload, and what makes a metric/log monkey's baseline fire."""
    d = FaultDriver(client)
    log: list[str] = []
    if not d.can_inject(fault):
        log.append(f"[drive] cannot synthesise a {fault} fault from the API — it needs real "
                   f"service traffic. Run examples/canary_service.py under OneAgent and hit it "
                   f"(see dashboards/README.md), then run the monkey.")
        return 0, log
    sent = 0
    for i in range(max(1, count)):
        d.inject(fault, monkey, scope, run_id, phase=f"drive-{i + 1}")
        sent += 1
    log.append(f"[drive] injected {sent} {fault} fault(s) into {scope!r} (run {run_id})")
    return sent, log
