"""Applies a vetted plan, injects the fault, asks the oracle, then reverts.

The order is load-bearing:
  1. guard vets the plan (may refuse)             -> no writes yet
  2. baseline: inject the fault with NO mutation   -> must be detected, else INCONCLUSIVE
  3. journal + backup + apply the mutation
  4. inject the fault again (coupled to the canary scope)
  5. oracle: did a matching problem still open within the detection window?
  6. revert from the journal, then verify by re-reading the objects

Verdict is three-state on purpose:
  * KILLED       detection broke  -> the config was load-bearing
  * SURVIVED     detection held   -> the config was dead/redundant
  * INCONCLUSIVE couldn't decide  -> the baseline never fired, or the window
                                      elapsed without a usable answer
The tool never collapses INCONCLUSIVE into SURVIVED: a false "safe to delete" is
the one result a monitoring-assurance tool must never give.
"""

from __future__ import annotations

import time
from typing import Optional

from .backup import BackupVault
from .client import DynatraceClient
from .journal import Journal
from .models import ExperimentResult, Outcome, Plan, new_run_id
from .resources import apply_change
from .safety import Deadline, Guard, assert_executable


class Runner:
    def __init__(self, client: DynatraceClient, guard: Guard, journal: Journal,
                 vault: Optional[BackupVault] = None) -> None:
        self.client = client
        self.guard = guard
        self.journal = journal
        self.vault = vault

    def run(self, plan: Plan, ttl_seconds: int = 300,
            oracle_wait_s: float = 0.0, poll_s: float = 2.0) -> ExperimentResult:
        run_id = new_run_id()
        scope = plan.probe_scope or (plan.changes[0].target.scope if plan.changes else "environment")
        msgs: list[str] = []

        # 0. The permanent off-switch, before anything else touches the plan.
        assert_executable(plan.monkey)

        # 1. Vet. Quarantined / too-wide / out-of-fence plans raise here.
        self.guard.vet(plan)
        msgs.append(f"vetted: {plan.monkey} ({plan.reversibility}/{plan.blast}) scope={scope}")

        if self.guard.dry_run:
            for ch in plan.changes:
                msgs.append(f"[dry-run] would change {ch.target}: {ch.note}")
            msgs.append(f"[dry-run] would inject: {plan.fault}")
            msgs.append(f"[dry-run] would watch {scope} for: {plan.expected_signal}")
            return ExperimentResult(plan.monkey, run_id, executed=False, detected=None,
                                    killed=None, reverted=False, revert_verified=False,
                                    messages=msgs, outcome=None)

        deadline = Deadline(min(ttl_seconds, self.guard.fence.max_ttl_seconds))
        detected: Optional[bool] = None
        outcome = Outcome.INCONCLUSIVE
        self._marker(plan, run_id, scope, "started")
        try:
            # 2. Baseline — prove the fault is detectable before we break anything.
            if not self._detect(plan, scope, run_id, oracle_wait_s, poll_s):
                msgs.append("BASELINE did not fire: fault not detectable even before the "
                            "mutation -> INCONCLUSIVE (fix the probe/window; nothing was changed)")
                return ExperimentResult(plan.monkey, run_id, executed=False, detected=False,
                                        killed=None, reverted=False, revert_verified=False,
                                        messages=msgs, outcome=Outcome.INCONCLUSIVE)
            msgs.append("baseline ok: fault is detectable on the canary scope")

            # 3. Apply (journal write-ahead -> full backup of removals -> mutate).
            for ch in plan.changes:
                self.journal.record(ch)
                if self.vault is not None:
                    saved = self.vault.backup(self.client, ch, run_id)
                    if saved:
                        msgs.append(f"backed up: {ch.target} -> {saved}")
                self._apply(ch)
                msgs.append(f"applied: {ch.target} ({ch.note})")

            # 4 + 5. Inject again and observe, within the detection window.
            detected = self._detect(plan, scope, run_id, oracle_wait_s, poll_s)
            outcome = Outcome.SURVIVED if detected else Outcome.KILLED
            msgs.append(f"after mutation: detected={detected} -> {outcome} "
                        + ("(detection held — config is dead/redundant)" if detected
                           else "(detection broke — config was load-bearing)"))
        finally:
            # 6. Always revert, then verify by re-reading the objects.
            undone = self.journal.restore(self.client)
            msgs.extend(f"reverted: {u}" for u in undone)
            verified = self._verify_revert(plan)
            msgs.append("revert verified (objects match pre-state)" if verified
                        else "WARNING: revert could not be verified against the tenant")
            if deadline.expired():
                msgs.append("note: TTL expired during run")
            self._marker(plan, run_id, scope, f"ended:{outcome}")

        return ExperimentResult(plan.monkey, run_id, executed=True, detected=detected,
                                killed=(outcome is Outcome.KILLED), reverted=True,
                                revert_verified=verified, messages=msgs, outcome=outcome)

    # --- helpers ------------------------------------------------------------

    def _apply(self, ch) -> None:
        new_id = apply_change(self.client, ch)      # dispatches on target kind
        if new_id:
            object.__setattr__(ch.target, "object_id", new_id)  # so restore can undo it

    def _fault_event(self, plan: Plan, scope: str, run_id: str) -> dict:
        """A fault coupled to this run and scope, so the oracle can attribute it."""
        return {
            "eventType": "ERROR_EVENT",
            "title": f"chaos:{plan.monkey}",
            "properties": {"chaos": "true", "chaos.run_id": run_id,
                           "chaos.monkey": plan.monkey, "chaos.scope": scope},
        }

    def _marker(self, plan: Plan, run_id: str, scope: str, phase: str) -> None:
        """Dogfood: drop a CUSTOM_INFO marker on the tenant timeline for this run."""
        try:
            self.client.ingest_event({
                "eventType": "CUSTOM_INFO",
                "title": f"Chaos experiment {phase}: {plan.monkey}",
                "properties": {"chaos.run_id": run_id, "chaos.phase": phase,
                               "chaos.monkey": plan.monkey, "chaos.scope": scope},
            })
        except Exception:
            pass  # a timeline marker must never be the thing that fails a run

    def _detect(self, plan: Plan, scope: str, run_id: str,
                wait_s: float, poll_s: float) -> bool:
        """Inject the coupled fault, then poll for a matching problem until the
        detection window elapses. Checks at least once even when wait_s == 0."""
        since = int(time.time() * 1000)
        self.client.ingest_event(self._fault_event(plan, scope, run_id))
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            problems = self.client.recent_problems(since)
            if any(self._matches(p, scope, run_id) for p in problems):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.1, poll_s))

    @staticmethod
    def _matches(problem: dict, scope: str, run_id: str) -> bool:
        """A problem counts if it carries our run marker or sits on the canary scope."""
        blob = str(problem).lower()
        if "chaos" in blob or run_id.lower() in blob:
            return True
        zones = problem.get("managementZones") or problem.get("affectedEntities") or []
        return any(scope.lower() in str(z).lower() for z in zones)

    def _verify_revert(self, plan: Plan) -> bool:
        """The journal says it undid everything; prove it by re-reading objects.

        For a settings object with a `before`, re-fetch it and compare values.
        Creates (undone by delete) and token recreates (new secret) can't be
        value-diffed, so those fall back to the journal's own accounting.
        """
        if self.journal.pending() != 0:
            return False
        try:
            for ch in plan.changes:
                t = ch.target
                if t.kind == "settings" and ch.before is not None and t.object_id:
                    current = self.client.get_object(t.object_id)
                    if current.get("value") != ch.before.get("value"):
                        return False
        except Exception:
            return False
        return True
