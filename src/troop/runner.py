"""Applies a vetted plan, injects the fault, asks the oracle, then reverts.

The order is load-bearing:
  1. guard vets the plan (may refuse)             -> no writes yet
  2. baseline: inject the fault with NO mutation   -> must be detected, else INCONCLUSIVE
  3. journal + backup + apply the mutation
  4. inject the fault again (coupled to the canary scope)
  5. oracle: did a *new* matching problem open within the detection window?
  6. revert from the journal, then verify by re-reading the objects

Verdict is three-state on purpose:
  * KILLED       detection broke  -> the config was load-bearing
  * SURVIVED     detection held   -> the config was dead/redundant
  * INCONCLUSIVE couldn't decide  -> the baseline never fired, or the window
                                      elapsed without a usable answer
The tool never collapses INCONCLUSIVE into SURVIVED: a false "safe to delete" is
the one result a monitoring-assurance tool must never give.

Oracle attribution (step 5) is deliberately strict, because the whole value of
the tool is that its verdict means something:
  * a problem is only credited if it carries this run's id, sits on the canary
    scope by *exact* management-zone match, or is chaos-tagged — not merely
    because the scope string appears somewhere in its JSON;
  * a problem that was already open at baseline is excluded from the post-
    mutation check, so a lingering baseline problem can't be mistaken for fresh
    detection (which would wrongly read as SURVIVED);
  * a problem that opened before we injected the fault (beyond a clock-skew
    tolerance) is ignored, for the same reason;
  * when the verdict is KILLED but a baseline problem is still open, the run
    says so — Dynatrace may have merged the re-injected fault into the existing
    problem instead of opening a new one, and the operator should verify.
"""

from __future__ import annotations

import time
from typing import Optional

from .backup import BackupVault
from .client import DynatraceClient
from .driver import FaultDriver
from .journal import Journal
from .models import ExperimentResult, Fault, Outcome, Plan, new_run_id
from .resources import apply_change
from .safety import Deadline, Guard, assert_executable

# A problem that opened more than this many ms before we injected the fault is
# treated as pre-existing, not as detection of our fault. Generous on purpose,
# so tenant/local clock skew never discards a genuinely fresh problem.
_OPENED_SKEW_MS = 120_000


class Runner:
    def __init__(self, client: DynatraceClient, guard: Guard, journal: Journal,
                 vault: Optional[BackupVault] = None) -> None:
        self.client = client
        self.guard = guard
        self.journal = journal
        self.vault = vault
        self.driver = FaultDriver(client)
        self._last_problems: list[dict] = []   # most recent oracle snapshot (for lingering check)

    def run(self, plan: Plan, ttl_seconds: int = 300,
            oracle_wait_s: float = 0.0, poll_s: float = 2.0,
            fault_kind: Fault = Fault.EVENT) -> ExperimentResult:
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
            if not self.driver.can_inject(fault_kind):
                msgs.append(f"this monkey's fault is {fault_kind} — the tool can't synthesise it "
                            f"(it needs real service traffic / an out-of-band fault). Drive the "
                            f"canary workload yourself (examples/canary_service.py or `troop "
                            f"drive`), or the baseline can't fire.")
            baseline = self._detect(plan, scope, run_id, oracle_wait_s, poll_s,
                                    phase="baseline", fault_kind=fault_kind)
            if not baseline:
                msgs.append("BASELINE did not fire: fault not detectable even before the "
                            "mutation -> INCONCLUSIVE (fix the probe/window, or drive the real "
                            "workload for a gated monkey; nothing was changed)")
                return ExperimentResult(plan.monkey, run_id, executed=False, detected=False,
                                        killed=None, reverted=False, revert_verified=False,
                                        messages=msgs, outcome=Outcome.INCONCLUSIVE)
            # Only problems with a stable id can be excluded later; anonymous
            # matches (ids the tenant didn't give us) can't be de-duplicated.
            baseline_ids = {h for h in baseline if not str(h).startswith("anon:")}
            msgs.append(f"baseline ok: fault detected ({len(baseline)} problem(s); "
                        f"{len(baseline_ids)} with a stable id, excluded post-mutation)")

            # 3. Apply (journal write-ahead -> full backup of removals -> mutate).
            for ch in plan.changes:
                self.journal.record(ch)
                if self.vault is not None:
                    saved = self.vault.backup(self.client, ch, run_id)
                    if saved:
                        msgs.append(f"backed up: {ch.target} -> {saved}")
                self._apply(ch)
                msgs.append(f"applied: {ch.target} ({ch.note})")

            # 4 + 5. Inject again and observe, crediting only a *new* problem.
            post = self._detect(plan, scope, run_id, oracle_wait_s, poll_s,
                                phase="mutation", exclude=baseline_ids, fault_kind=fault_kind)
            detected = bool(post)
            outcome = Outcome.SURVIVED if detected else Outcome.KILLED
            msgs.append(f"after mutation: detected={detected} -> {outcome} "
                        + ("(detection held — config is dead/redundant)" if detected
                           else "(detection broke — config was load-bearing)"))
            if not detected:
                msgs.append("CAUTION: KILLED is a v0 signal, not proof. Verify by hand and do "
                            "NOT wire it into automated remediation, paging, or config deletion.")
                lingering = baseline_ids & self._open_ids(self._last_problems)
                if lingering:
                    msgs.append("note: baseline problem(s) " + ", ".join(sorted(lingering))
                                + " are still open — Dynatrace may have merged the re-injected "
                                "fault into them instead of opening a fresh problem. Verify this "
                                "KILLED before acting on it, or use a fault that opens a distinct "
                                "problem.")
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

    def _detect(self, plan: Plan, scope: str, run_id: str, wait_s: float, poll_s: float,
                phase: str, exclude: frozenset[str] = frozenset(),
                fault_kind: Fault = Fault.EVENT) -> set[str]:
        """Inject the coupled fault (via the driver), then poll for a matching
        problem until the detection window elapses.

        Returns the set of attributed problem keys (a stable id where the tenant
        gives one, else an ``anon:`` sentinel). Checks at least once even when
        ``wait_s == 0``. Problems in ``exclude`` (already open at baseline) and
        problems that opened before we injected are never credited. If the fault
        kind isn't injectable, nothing is sent — the oracle then relies on a
        workload the operator drove out of band.
        """
        inject_ms = int(time.time() * 1000)
        self.driver.inject(fault_kind, plan.monkey, scope, run_id, phase)
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            problems = self.client.recent_problems(inject_ms)
            self._last_problems = problems
            hits = self._attribute(problems, scope, run_id, inject_ms, exclude)
            if hits:
                return hits
            if time.monotonic() >= deadline:
                return set()
            time.sleep(max(0.1, poll_s))

    def _attribute(self, problems: list[dict], scope: str, run_id: str,
                   inject_ms: int, exclude: frozenset[str]) -> set[str]:
        """Credit only problems that are ours, new, and not already open."""
        hits: set[str] = set()
        for i, p in enumerate(problems):
            pid = self._problem_id(p)
            if pid is not None and pid in exclude:
                continue                       # already open at baseline
            if not self._opened_in_window(p, inject_ms):
                continue                       # pre-existing, opened before our fault
            if not self._matches(p, scope, run_id):
                continue
            hits.add(pid if pid is not None else f"anon:{run_id}:{i}")
        return hits

    @staticmethod
    def _problem_id(problem: dict) -> Optional[str]:
        pid = problem.get("problemId") or problem.get("displayId") or problem.get("id")
        return str(pid) if pid is not None else None

    @staticmethod
    def _opened_in_window(problem: dict, inject_ms: int) -> bool:
        """True unless the problem clearly opened before we injected the fault."""
        start = problem.get("startTime", problem.get("start"))
        if start in (None, ""):
            return True                        # no timestamp -> can't exclude it
        try:
            return int(start) >= inject_ms - _OPENED_SKEW_MS
        except (TypeError, ValueError):
            return True

    @staticmethod
    def _matches(problem: dict, scope: str, run_id: str) -> bool:
        """Attribute a problem to this run: by run id, exact MZ, or chaos tag.

        Note this is stricter than a substring scan: the scope must match a
        management zone's id or name exactly, not merely appear somewhere in the
        problem's JSON, so an unrelated problem that happens to mention the scope
        string isn't credited.
        """
        blob = str(problem).lower()
        if run_id.lower() in blob:
            return True
        s = scope.lower()
        zones = problem.get("managementZones") or problem.get("affectedEntities") or []
        for z in zones:
            if isinstance(z, dict):
                if s == str(z.get("id", "")).lower() or s == str(z.get("name", "")).lower():
                    return True
            elif s == str(z).lower():
                return True
        return "chaos" in blob

    @staticmethod
    def _open_ids(problems: list[dict]) -> set[str]:
        ids: set[str] = set()
        for p in problems:
            pid = p.get("problemId") or p.get("displayId") or p.get("id")
            if pid is not None:
                ids.add(str(pid))
        return ids

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
