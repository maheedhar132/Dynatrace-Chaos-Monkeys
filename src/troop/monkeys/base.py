"""What every fire-capable monkey implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..client import DynatraceClient
from ..models import Blast, Plan, Reversibility


class Monkey(ABC):
    """A single kind of chaos.

    A monkey only knows how to *plan*: given a client (to read the current
    config) and a scope, it returns a `Plan` of reversible changes plus the
    fault it would inject. The runner is what actually applies, injects,
    observes and reverts — and only after `Guard.vet` approves the plan.
    """

    name: str = "monkey"
    reversibility: Reversibility = Reversibility.AUTO
    blast: Blast = Blast.OBJECT

    @abstractmethod
    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        """Describe the experiment without touching the tenant."""
        raise NotImplementedError
