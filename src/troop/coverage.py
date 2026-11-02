"""Which monkeys are live now, which are gated, and do their schemas exist?

Two runtime-truth helpers:

  * `classify()` — for every fire monkey, the fault kind that drives it and the
    derived live/gated bucket. A fault the driver can synthesise (EVENT / METRIC
    / LOG) means the monkey is live now; TRACE needs real service traffic
    (the reference canary); NONE is out-of-band. Surfaced in `troop list`,
    `plan`, `run`, and `rampage` so a gated monkey never silently wastes a run.

  * `verify_schemas()` — prove each monkey's Settings 2.0 schema exists on the
    tenant, and (where the canary kit knows the shape) validate it against the
    live schema *without writing it*: `troop verify --tenant <live>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .canary import default_specs
from .client import DynatraceClient
from .driver import INJECTABLE
from .models import Drive, Fault
from .monkeys import FIRE

# Pseudo "schemas" that aren't Settings 2.0 objects — verified via other APIs.
_NON_SETTINGS = {"api-token", "synthetic-monitor"}


def drive_of(fault: Fault) -> Drive:
    """The coarse live/gated bucket for a fault kind."""
    if fault in INJECTABLE:
        return Drive.EVENT       # the driver can synthesise it -> live now
    if fault is Fault.TRACE:
        return Drive.TRAFFIC     # needs real service traffic
    return Drive.EXTERNAL        # out-of-band fault


@dataclass(frozen=True)
class Classification:
    name: str
    fault: Fault
    schemas: tuple[str, ...]

    @property
    def drive(self) -> Drive:
        return drive_of(self.fault)

    @property
    def live(self) -> bool:
        return self.fault.injectable


def classify(names: Optional[list[str]] = None) -> list[Classification]:
    """Fault + schemas for each fire monkey, in registry order."""
    chosen = names or list(FIRE)
    out: list[Classification] = []
    for n in chosen:
        cls = FIRE[n]
        out.append(Classification(n, getattr(cls, "fault", Fault.EVENT),
                                  tuple(getattr(cls, "schemas", ()))))
    return out


def live_names() -> list[str]:
    return [c.name for c in classify() if c.live]


def gated_names() -> list[str]:
    return [c.name for c in classify() if not c.live]


def fault_note(fault: Fault, name: str) -> Optional[str]:
    """A one-line runtime explanation for a gated monkey (None when live)."""
    if fault is Fault.TRACE:
        return (f"{name} is TRACE-gated: it needs real request traffic against an "
                f"OneAgent-monitored canary. A synthetic event/metric won't exercise it. Run "
                f"examples/canary_service.py (or your own workload) against the canary, then "
                f"retry — otherwise expect INCONCLUSIVE.")
    if fault is Fault.NONE:
        return (f"{name} is out-of-band: its fault originates outside Dynatrace (a dead token, "
                f"a frozen agent, a synthetic run). Nothing can inject it; expect INCONCLUSIVE "
                f"without the real fault.")
    return None


@dataclass
class SchemaCheck:
    monkey: str
    schema_id: str
    status: str          # "present" | "valid" | "invalid" | "missing" | "n/a"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("present", "valid", "n/a")


def _value_by_schema() -> dict[str, object]:
    """Sample values the canary kit knows how to build, keyed by schema, for
    validate-only checks. Scope is filled in by the caller."""
    return {spec.schema_id: spec.value for spec in default_specs()}


def verify_schemas(client: DynatraceClient, scope: str = "environment",
                   names: Optional[list[str]] = None) -> list[SchemaCheck]:
    """Prove each monkey's schema against the tenant `client` points at.

    Presence is checked against the tenant's schema catalog; where the canary kit
    has a sample shape, it is additionally validated (validate-only, never
    written). Against a dry-run client this reports the schemas the troop knows,
    clearly as a dry-run (no real tenant was consulted).
    """
    try:
        available = set(client.schema_ids())
    except Exception as exc:
        return [SchemaCheck("(tenant)", "-", "missing", f"could not list schemas: {exc}")]

    samples = _value_by_schema()
    checks: list[SchemaCheck] = []
    for c in classify(names):
        if not c.schemas:
            continue
        for schema in c.schemas:
            if schema in _NON_SETTINGS:
                checks.append(SchemaCheck(c.name, schema, "n/a",
                                          "not a Settings 2.0 schema (uses the tokens/synthetic API)"))
                continue
            if schema not in available:
                checks.append(SchemaCheck(c.name, schema, "missing",
                                          "schema not present on this tenant/version"))
                continue
            if schema in samples:
                errs = client.validate_object(schema, scope, samples[schema](scope))
                if errs:
                    checks.append(SchemaCheck(c.name, schema, "invalid", "; ".join(errs)[:200]))
                else:
                    checks.append(SchemaCheck(c.name, schema, "valid", "shape accepted (validate-only)"))
            else:
                checks.append(SchemaCheck(c.name, schema, "present", "schema exists"))
    return checks
