"""What every fire-capable monkey implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..client import DynatraceClient
from ..models import Blast, Fault, Plan, Reversibility


class Monkey(ABC):
    """A single kind of chaos.

    A monkey only knows how to *plan*: given a client (to read the current
    config) and a scope, it returns a `Plan` of reversible changes plus the
    fault it would inject. The runner is what actually applies, injects,
    observes and reverts — and only after `Guard.vet` approves the plan.

    `fault` is the kind of fault that drives its baseline (EVENT / METRIC / LOG
    are injectable, so the monkey is live now; TRACE needs a real workload; NONE
    is out-of-band). `schemas` is the Settings 2.0 schema id(s) it touches, used
    by `troop verify`. Both are surfaced at runtime so a gated monkey never
    silently wastes a run.
    """

    name: str = "monkey"
    reversibility: Reversibility = Reversibility.AUTO
    blast: Blast = Blast.OBJECT
    fault: Fault = Fault.EVENT
    schemas: tuple[str, ...] = ()

    @abstractmethod
    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        """Describe the experiment without touching the tenant."""
        raise NotImplementedError
