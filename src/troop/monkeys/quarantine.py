"""The dangerous ideas — modeled, classified, and never executed.

These are the actions that are irreversible, fleet-wide, or purely financial.
They exist here so the taxonomy and the destructiveness index are honest and so
a reviewer can see exactly what the tool knows how to refuse. There is no code
in this module that performs any of them: a `QuarantinedMonkey` has no `plan`
that touches the tenant, and `Guard.vet` raises `ForbiddenAction` on the name
alone (see safety.py `_QUARANTINE_NAMES`).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Blast, Reversibility, Verdict


@dataclass(frozen=True)
class QuarantinedMonkey:
    name: str
    what: str            # what it would do
    why_blocked: str     # why it is never run
    reversibility: Reversibility
    blast: Blast
    verdict: Verdict = Verdict.QUARANTINE

    def plan(self, *_args, **_kwargs):
        raise RuntimeError(
            f"'{self.name}' is quarantined and has no executable plan: {self.why_blocked}"
        )


QUARANTINE: dict[str, QuarantinedMonkey] = {
    "gorilla": QuarantinedMonkey(
        name="gorilla",
        what="Mass-downgrade monitoring mode / disable OneAgent features across a whole scope.",
        why_blocked="Re-enabling deep monitoring needs process restarts fleet-wide; "
                    "there is no clean programmatic revert. Blast escalates to TENANT.",
        reversibility=Reversibility.MANUAL, blast=Blast.TENANT,
    ),
    "kong": QuarantinedMonkey(
        name="kong",
        what="Rotate the tenant token.",
        why_blocked="Disconnects every OneAgent at once. 'Finishing' the rotation does not "
                    "reconnect agents — the new token must be pushed to each. No clean revert; "
                    "blast is FLEET. This is the single most destructive action available.",
        reversibility=Reversibility.NONE, blast=Blast.FLEET,
    ),
    "leech": QuarantinedMonkey(
        name="leech",
        what="Drive licensing/consumption cost up via bulk or repeated DQL queries.",
        why_blocked="Consumed DDUs/DPS are not refundable. Pure financial harm with no "
                    "resilience hypothesis to test.",
        reversibility=Reversibility.NONE, blast=Blast.FLEET,
    ),
    "locust": QuarantinedMonkey(
        name="locust",
        what="Create unfiltered, tenant-wide alerting profiles + integrations that alert on everything.",
        why_blocked="Floods on-call across the whole tenant. An operational-noise weapon, not a "
                    "scoped experiment. The scoped, reversible version is the Howler fire monkey.",
        reversibility=Reversibility.AUTO, blast=Blast.TENANT,
    ),
    "reaper": QuarantinedMonkey(
        name="reaper",
        what="Hard-delete config at scale, delete Grail buckets, run anonymization, or uninstall agents.",
        why_blocked="Destroys data, history, or agents irreversibly. Blast TENANT→FLEET, "
                    "reversibility NONE.",
        reversibility=Reversibility.NONE, blast=Blast.FLEET,
    ),
}
