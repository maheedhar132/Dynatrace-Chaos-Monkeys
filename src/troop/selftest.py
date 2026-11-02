"""`troop selftest`: prove the live faults actually open a problem on THIS tenant.

The honest risk after everything else is built: the provisioned detectors (the
metric event, the log event) are representative shapes that have never fired on a
real tenant. A shape can be schema-valid and still not trip — so the one thing
you can't know without trying is whether injecting a fault actually opens a
problem *here*.

Self-test answers exactly that, end to end and non-destructively (it injects
faults and reads problems; it mutates no config). For each live monkey it drives
the monkey's fault and checks that a matching problem opened within the window.
`PASS` means the whole loop works on your tenant — the detector is configured,
the metric/log/event lands, Davis opens a problem, and the oracle attributes it.
A `FAIL` points you straight at the detector or push cadence to tune (see
docs/detectors.md). Run it once and "never run live" is retired.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .client import DynatraceClient
from .coverage import classify
from .driver import FaultDriver
from .models import Fault, new_run_id
from .runner import Runner


@dataclass
class SelfCheck:
    monkey: str
    fault: Fault
    fired: bool
    detail: str


def _probe(client: DynatraceClient, monkey: str, fault: Fault, scope: str, run_id: str,
           wait_s: float, poll_s: float, burst: int = 3) -> bool:
    """Inject the fault (a small burst for metric/log, so the detector's window
    has data) and poll for a matching problem. No config is mutated."""
    driver = FaultDriver(client)
    inject_ms = int(time.time() * 1000)
    shots = burst if fault in (Fault.METRIC, Fault.LOG) else 1
    injected = False
    for i in range(max(1, shots)):
        if driver.inject(fault, monkey, scope, run_id, phase=f"selftest-{i + 1}"):
            injected = True
    if not injected:
        return False
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        for p in client.recent_problems(inject_ms):
            if Runner._opened_in_window(p, inject_ms) and Runner._matches(p, scope, run_id):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.1, poll_s))


def run_selftest(client: DynatraceClient, scope: str, *, oracle_wait: float = 120.0,
                 poll: float = 5.0, names: Optional[list[str]] = None) -> list[SelfCheck]:
    """Probe every live (injectable) monkey's detector. Gated monkeys are skipped
    — they need a real workload, which `troop run --drive-canary` provides."""
    checks: list[SelfCheck] = []
    for c in classify(names):
        if not c.live:
            continue
        fired = _probe(client, c.name, c.fault, scope, new_run_id(), oracle_wait, poll)
        checks.append(SelfCheck(
            c.name, c.fault, fired,
            "problem opened - detector fires" if fired
            else "no matching problem within the window - tune the detector / push (docs/detectors.md)"))
    return checks
