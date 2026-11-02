"""Core domain types shared across the troop.

The two enums below are the whole safety story in miniature: a monkey may only
fire when its action is reversible enough and its blast radius small enough.
`Guard` (see safety.py) enforces that; everything else just carries the data.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


class Reversibility(enum.IntEnum):
    """How hard it is for the tool to put things back. Lower is safer."""

    AUTO = 0      # tool reverts from a snapshot, cleanly
    MANUAL = 1    # a human can fix it, but the tool can't (restart / re-auth)
    NONE = 2      # no clean undo; data or trust is gone

    def __str__(self) -> str:  # nicer CLI output
        return self.name


class Blast(enum.IntEnum):
    """How far the damage spreads. Lower is safer."""

    OBJECT = 0    # one settings object / one entity
    SCOPE = 1     # one management zone / host group / segment
    TENANT = 2    # the whole environment
    FLEET = 3     # every OneAgent, or billing

    def __str__(self) -> str:
        return self.name


class Verdict(enum.Enum):
    FIRE = "fire"              # reversible + scoped; a real experiment
    QUARANTINE = "quarantine"  # irreversible / fleet-wide / financial; never executed

    def __str__(self) -> str:
        return self.value


class Fault(enum.Enum):
    """The kind of fault that drives a monkey's baseline — what the fault driver
    must inject for detection to even have a chance to fire.

    EVENT, METRIC and LOG can be injected synthetically over the Dynatrace ingest
    APIs (events / metrics / logs), so those monkeys are **live now** — the driver
    drives them end to end. TRACE needs real request traffic against an
    OneAgent-monitored service (run the reference canary in `examples/`, or
    `troop drive` your own). NONE means the fault originates entirely outside
    Dynatrace (a dead token, a frozen agent, a synthetic monitor that must run)
    and nothing can inject it. A monkey whose fault is not injectable returns
    INCONCLUSIVE from the tool alone — surfaced at runtime, never a false verdict.
    """

    EVENT = "event"      # an ingested ERROR_EVENT opens a problem
    METRIC = "metric"    # an ingested canary metric crosses a detector threshold
    LOG = "log"          # an ingested canary log line hits a log pipeline
    TRACE = "trace"      # real service traffic (needs an OneAgent-monitored canary)
    NONE = "none"        # the fault happens outside Dynatrace; nothing to inject

    def __str__(self) -> str:
        return self.name

    @property
    def injectable(self) -> bool:
        """True when the fault driver can synthesize this fault (so it's live now)."""
        return self in (Fault.EVENT, Fault.METRIC, Fault.LOG)


class Drive(enum.Enum):
    """The coarse live-vs-gated bucket, derived from a monkey's `Fault` kind.

    EVENT = the driver can exercise it now; TRAFFIC = needs a real workload
    (TRACE fault); EXTERNAL = the fault is out-of-band (NONE). See `coverage`.
    """

    EVENT = "event"        # live now: the fault driver injects its fault
    TRAFFIC = "traffic"    # gated: needs real service traffic (reference canary)
    EXTERNAL = "external"  # gated: the fault originates outside Dynatrace

    def __str__(self) -> str:
        return self.name

    @property
    def live(self) -> bool:
        return self is Drive.EVENT

    @property
    def label(self) -> str:
        return "live" if self is Drive.EVENT else "gated"


class Outcome(enum.Enum):
    """The result of one experiment. The third state is the important one.

    A monitoring-assurance tool that silently turns "I didn't wait long enough"
    into "this config is dead weight" is worse than useless, so a run that cannot
    be decided is INCONCLUSIVE — never quietly SURVIVED.
    """

    KILLED = "killed"              # mutation broke detection -> the config was load-bearing
    SURVIVED = "survived"          # detection still fired     -> the config was dead/redundant
    INCONCLUSIVE = "inconclusive"  # couldn't decide (baseline never fired, timeout, or error)

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Target:
    """A single thing a monkey touches, named the way the API names it.

    `kind` says which resource family it belongs to, so the journal knows how to
    apply and undo it: "settings" (the default, a Settings 2.0 object), "token"
    (an API token), or "synthetic" (a synthetic monitor). See resources.py.
    """

    schema_id: str                       # e.g. "builtin:alerting.profile" (or "api-token")
    object_id: Optional[str] = None      # a settings objectId / token id, or None when creating
    scope: str = "environment"           # settings scope: environment / HOST-xxx / mz id / pseudo-scope
    label: str = ""                      # human label for logs
    kind: str = "settings"               # "settings" | "token" | "synthetic"

    def __str__(self) -> str:
        who = self.object_id or "(new)"
        tag = "" if self.kind == "settings" else f" {self.kind}"
        return f"{self.schema_id}[{who}]@{self.scope}{tag}"


@dataclass
class Change:
    """One reversible edit, with everything needed to undo it.

    `before` is None for a create (undo = delete the created object).
    `after` is None for a delete (undo = recreate `before`).
    """

    target: Target
    before: Optional[dict[str, Any]]
    after: Optional[dict[str, Any]]
    note: str = ""


@dataclass
class Plan:
    """What a monkey intends to do, produced without touching the tenant."""

    monkey: str
    description: str
    reversibility: Reversibility
    blast: Blast
    changes: list[Change] = field(default_factory=list)
    fault: str = ""            # the known fault that will be injected
    expected_signal: str = ""  # what detection should produce if config is healthy
    probe_scope: Optional[str] = None  # the MZ/scope the fault + oracle target (coupling)


@dataclass
class ExperimentResult:
    monkey: str
    run_id: str
    executed: bool
    detected: Optional[bool]   # None = not evaluated (dry run / baseline failed)
    killed: Optional[bool]     # True = mutation broke detection (load-bearing config)
    reverted: bool
    revert_verified: bool
    messages: list[str] = field(default_factory=list)
    outcome: Optional[Outcome] = None  # KILLED / SURVIVED / INCONCLUSIVE (experiments)


def new_run_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
