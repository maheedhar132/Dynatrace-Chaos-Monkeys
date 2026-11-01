"""Watchers: monkeys that make a tiny reversible change and then *assert a signal
appeared*, instead of asking whether detection survived a break.

Where a fire monkey answers "did monitoring still catch the fault?", a watcher
answers "was this change even observable?" — the question the audit section of
docs/monkey-lineup.md (§A, 'recorded ≠ noticed') is really about.

A watcher still goes through the full safety machinery: the permanent off-switch,
the guard's canary fence, and the write-ahead journal that reverts it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from ..client import DynatraceClient
from ..journal import Journal
from ..models import Change, ExperimentResult, Plan, Target, new_run_id
from ..safety import Guard, assert_executable
from .fire import CANARY_TAG


class Watcher(ABC):
    name: str = "watcher"

    @abstractmethod
    def probe(self, client: DynatraceClient, scope: str) -> Plan:
        """A benign, reversible canary change whose only purpose is to be seen."""
        raise NotImplementedError

    @abstractmethod
    def saw_signal(self, client: DynatraceClient, since_ms: int, plan: Plan) -> bool:
        """Did the expected signal (audit event, meta-alert, drift flag) appear?"""
        raise NotImplementedError

    def run(self, client: DynatraceClient, guard: Guard, journal: Journal, scope: str,
            timeout_s: float = 120.0, poll_s: float = 5.0) -> ExperimentResult:
        run_id = new_run_id()
        msgs: list[str] = []
        assert_executable(self.name)
        plan = self.probe(client, scope)
        guard.vet(plan)
        msgs.append(f"vetted watcher {self.name}: probe = {plan.description}")

        if guard.dry_run:
            for ch in plan.changes:
                msgs.append(f"[dry-run] would probe with {ch.target}: {ch.note}")
            msgs.append(f"[dry-run] would then watch for: {plan.expected_signal}")
            return ExperimentResult(self.name, run_id, executed=False, detected=None,
                                    killed=None, reverted=False, revert_verified=False,
                                    messages=msgs)

        since_ms = int(time.time() * 1000)
        try:
            for ch in plan.changes:
                journal.record(ch)
                if ch.before is None and ch.after is not None:
                    oid = client.create_object(ch.target.schema_id, ch.target.scope,
                                               ch.after.get("value", ch.after))
                    object.__setattr__(ch.target, "object_id", oid)
                elif ch.target.object_id:
                    client.update_object(ch.target.object_id, ch.after.get("value", ch.after))
                msgs.append(f"probed: {ch.target} ({ch.note})")

            seen = self._wait_for_signal(client, since_ms, plan, timeout_s, poll_s)
            msgs.append(f"signal seen: {seen} -> "
                        + ("OK (change is observable)" if seen
                           else "BLIND SPOT (change was not recorded/noticed)"))
        finally:
            undone = journal.restore(client)
            msgs.extend(f"reverted: {u}" for u in undone)
            verified = journal.pending() == 0
            msgs.append("revert verified" if verified else "WARNING: revert not verified")

        # killed=True here means 'a gap was found' (the signal did NOT appear)
        return ExperimentResult(self.name, run_id, executed=True, detected=seen,
                                killed=not seen, reverted=True, revert_verified=verified,
                                messages=msgs)

    def _wait_for_signal(self, client, since_ms, plan, timeout_s, poll_s) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.saw_signal(client, since_ms, plan):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)


class Potto(Watcher):
    """Creates an inert canary auto-tag rule (a real settings change that matches
    nothing), then checks the audit log recorded it.

    A miss means your audit/meta-monitoring has a blind spot: config can change
    without leaving a trace anyone can alert on — gap #18 in the lineup.
    """

    name = "potto"

    def probe(self, client: DynatraceClient, scope: str) -> Plan:
        value = {"name": f"{CANARY_TAG} probe (auto)", "rules": [],  # matches nothing
                 "note": CANARY_TAG}
        change = Change(
            target=Target("builtin:tags.auto-tagging", None, scope,
                          label=f"{CANARY_TAG} audit probe"),
            before=None, after={"value": value}, note="create inert canary probe object",
        )
        from ..models import Blast, Reversibility
        return Plan(
            monkey=self.name, description="Create an inert canary object to probe the audit log",
            reversibility=Reversibility.AUTO, blast=Blast.OBJECT, changes=[change],
            fault="(watcher — no fault injected)",
            expected_signal="An AUDIT_EVENT (provider SETTINGS) appears for the change",
        )

    def saw_signal(self, client: DynatraceClient, since_ms: int, plan: Plan) -> bool:
        for e in client.audit_events(since_ms):
            blob = str(e).lower()
            if CANARY_TAG in blob or "tags.auto-tagging" in blob or "settings" in blob:
                return True
        return False
