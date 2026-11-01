"""The guard rail every monkey passes through before it touches anything.

Nothing in this project edits a tenant without clearing `Guard`. The checks are
deliberately boring and deliberately strict: a chaos tool earns the right to
break things only by proving it can never break the wrong thing.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .models import Blast, Plan, Reversibility, Target

# Schemas and endpoints that are never mutated, under any flag. These map to the
# QUARANTINE rows in docs/destructiveness-index.md.
HARD_DENYLIST: frozenset[str] = frozenset(
    {
        "tenant-token-rotation",          # disconnects every OneAgent
        "grail.bucket-definitions",       # data deletion
        "anonymization",                  # data deletion
        "oneagent.uninstall",
        "builtin:preferences.privacy",    # breaking these leaks PII, not visibility
        "builtin:preferences.ipaddressmasking",
        "builtin:attribute-masking",
    }
)


# The tenant-wreckers. A monkey whose name is (or contains) one of these can
# never run — there is deliberately NO flag, config value, or arming step that
# enables it. This is the hard off-switch the project is built around: the guard
# refuses them, the runner refuses them, the CLI refuses them, and the HTTP
# client has no method that could carry them out. See docs/destructiveness-index.md.
PERMANENTLY_DISABLED: frozenset[str] = frozenset(
    {
        "kong",      # tenant token rotation — disconnects every OneAgent, no clean revert
        "gorilla",   # mass monitoring-mode downgrade — fleet-wide, restart-to-recover
        "leech",     # bulk-DQL cost inflation — unrefundable financial harm
        "locust",    # unfiltered tenant-wide alert spam — on-call noise weapon
        "reaper",    # hard delete / bucket deletion / anonymization / agent uninstall
    }
)


class SafetyError(RuntimeError):
    """Base for anything the guard refuses."""


class ForbiddenAction(SafetyError):
    """A quarantined / permanently-disabled action tried to execute. Hard stop."""


def assert_executable(monkey_name: str) -> None:
    """The off-switch, callable from anywhere. Raise if this monkey may never run.

    Checks exact and substring matches against PERMANENTLY_DISABLED so a renamed
    or wrapped variant (e.g. 'kong2', 'super-kong') is caught too. There is no
    parameter to disable this check — that is the whole point.
    """
    name = monkey_name.lower()
    hit = next((d for d in PERMANENTLY_DISABLED if d == name or d in name), None)
    if hit:
        raise ForbiddenAction(
            f"'{monkey_name}' is PERMANENTLY DISABLED (matches {hit!r}): it maps to a "
            f"tenant-wrecking action such as tenant-token rotation. No flag, config, or "
            f"arming step enables it. See docs/destructiveness-index.md."
        )


class ScopeViolation(SafetyError):
    """A target fell outside the canary fence."""


class KillSwitchEngaged(SafetyError):
    """The kill switch file exists; stop and revert."""


@dataclass
class Fence:
    """Defines the only region of the tenant the troop may touch.

    A target is in-bounds only if its scope is one of these, or its label is
    tagged with `required_tag`. Everything else is refused before any write.
    """

    allowed_scopes: frozenset[str]          # canary MZ ids, host groups, "environment" is NOT allowed by default
    required_tag: str = "chaos-canary"
    kill_switch_path: str = ".troop-stop"
    max_ttl_seconds: int = 1800             # nothing may stay mutated longer than this

    def check_target(self, target: Target) -> None:
        """In-bounds requires BOTH an allowed scope AND the canary tag.

        Either alone is not enough: a canary-tagged object in a broad scope, or
        an untagged object that merely sits in the canary MZ, is still refused.
        """
        if target.scope not in self.allowed_scopes:
            raise ScopeViolation(
                f"{target} scope not in canary fence {sorted(self.allowed_scopes)}"
            )
        if self.required_tag not in target.label:
            raise ScopeViolation(
                f"{target} is missing the required tag {self.required_tag!r}"
            )

    def check_kill_switch(self) -> None:
        if os.path.exists(self.kill_switch_path):
            raise KillSwitchEngaged(
                f"kill switch present at {self.kill_switch_path!r}; refusing to run"
            )


@dataclass
class Guard:
    """Gatekeeper. `vet` is called on every plan before execution."""

    fence: Fence
    dry_run: bool = True
    allow_blast: Blast = Blast.SCOPE          # refuse anything wider
    allow_reversibility: Reversibility = Reversibility.MANUAL
    _audit: list[str] = field(default_factory=list)

    def vet(self, plan: Plan) -> None:
        """Raise if this plan must not execute. Never mutates anything."""
        self.fence.check_kill_switch()
        assert_executable(plan.monkey)          # the permanent off-switch, first

        if plan.blast > self.allow_blast:
            raise ForbiddenAction(
                f"'{plan.monkey}' blast radius {plan.blast} exceeds limit {self.allow_blast}"
            )
        if plan.reversibility > self.allow_reversibility:
            raise ForbiddenAction(
                f"'{plan.monkey}' reversibility {plan.reversibility} exceeds limit "
                f"{self.allow_reversibility}"
            )

        for change in plan.changes:
            self.fence.check_target(change.target)
            if change.target.schema_id in HARD_DENYLIST:
                raise ForbiddenAction(f"{change.target.schema_id} is on the hard denylist")

        self._audit.append(f"vetted {plan.monkey}: {len(plan.changes)} change(s), ok")

    @property
    def audit(self) -> list[str]:
        return list(self._audit)


@dataclass
class Deadline:
    """A TTL that the runner honours so nothing stays broken if a run dies."""

    seconds: int
    _start: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return (time.monotonic() - self._start) >= self.seconds

    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self._start))
