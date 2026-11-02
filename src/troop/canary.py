"""Bootstrap the canary fixtures a real run needs — and tear them down exactly.

The whole tool keys on a *canary*: a dedicated, clearly-tagged slice of the
tenant that the monkeys are allowed to break. Without it, the first thing a new
user sees is an empty plan ("no canary-tagged objects found") and they give up.
This module removes that cliff: `CanaryKit.setup` provisions

  * a dedicated **management zone** (`chaos-canary-zone`), whose Settings 2.0
    object id becomes the canary *scope* you put in `canary_scopes`, and
  * a small set of **canary-tagged objects** scoped to that zone, one per
    event-drivable monkey (an alerting profile, a problem notification, a Davis
    anomaly detector, an auto-tag rule) — each carrying the `chaos-canary` tag
    in its value, which is exactly what the guard fence and the monkeys look for.

Everything created is written to a **manifest** (`.troop-canary.json`) so
`teardown` removes precisely what setup made — newest first, the zone last — and
nothing else. Both honour dry-run: nothing is written to a tenant unless the
caller passes a live client.

Honesty about schemas: Settings 2.0 object shapes and which scopes a schema
accepts vary by tenant and version. Setup is therefore *resilient* — if the
tenant rejects one object it is recorded as skipped and the rest proceed, so you
get a partial canary and a clear report instead of a hard stop. Treat the shapes
below as representative and expect to tweak one or two for your tenant.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .client import DynatraceClient
from .driver import CANARY_METRIC

CANARY_TAG = "chaos-canary"
MZ_SCHEMA = "builtin:management-zones"


@dataclass(frozen=True)
class CanarySpec:
    """One canary object to create, scoped to the canary management zone."""

    key: str                                   # short id used in the manifest
    schema_id: str
    value: Callable[[str], dict[str, Any]]     # scope -> value dict (carries CANARY_TAG)
    note: str


def _mz_value() -> dict[str, Any]:
    return {
        "name": f"{CANARY_TAG}-zone",
        "description": "Isolated zone for Dynatrace chaos monkey experiments. Safe to delete.",
        "rules": [{
            "type": "ME", "enabled": True,
            "conditions": [{
                "key": {"attribute": "HOST_NAME"},
                "comparisonInfo": {"type": "STRING", "operator": "CONTAINS",
                                   "value": CANARY_TAG, "caseSensitive": False},
            }],
        }],
        "note": CANARY_TAG,
    }


def default_specs() -> list[CanarySpec]:
    """The canary objects a live run needs — both the *mutation targets* the
    monkeys break and the *matching detectors* that make a fault actually open a
    problem. Without the detectors, the METRIC/LOG monkeys would be injectable
    but inert (the fault lands but nothing watches for it), so the baseline would
    never fire. These provision the watcher too:

      * metric-event  — opens a problem when the driver pushes `chaos.canary.fault`
                        (marmoset disables THIS)
      * log-event     — opens a problem when a `chaos-canary` log line lands
                        (colobus's exclusion is meant to blind it)
      * log-storage   — the storage rule colobus actually mutates

    TRACE monkeys (service AD, failure detection, SLO, host mode) still need a
    real OneAgent-monitored workload — a bootstrap can't synthesise traffic — so
    those are driven by the reference canary (`troop run --drive-canary`).

    Shapes are representative; `troop verify` validates them against your tenant.
    """
    return [
        CanarySpec(
            "alerting-profile", "builtin:alerting.profile",
            lambda s: {"name": f"{CANARY_TAG} profile", "managementZone": s,
                       "severityRules": [{"severityLevel": "MONITORING_UNAVAILABLE",
                                          "delayInMinutes": 0}],
                       "note": CANARY_TAG},
            "canary alerting profile (baboon, howler)",
        ),
        CanarySpec(
            "problem-notification", "builtin:problem.notifications",
            lambda s: {"enabled": True, "displayName": f"{CANARY_TAG} notification",
                       "type": "WEBHOOK",
                       "webhook": {"url": "https://example.invalid/chaos-sink"},
                       "note": CANARY_TAG},
            "canary problem notification (ape)",
        ),
        CanarySpec(
            "auto-tag", "builtin:tags.auto-tagging",
            lambda s: {"name": f"{CANARY_TAG} tag",
                       "rules": [{"type": "ME", "enabled": True, "valueFormat": CANARY_TAG}],
                       "note": CANARY_TAG},
            "canary auto-tag rule (capuchin)",
        ),
        # --- matching detectors: these make a METRIC/LOG fault actually fire ---
        CanarySpec(
            "metric-event", "builtin:anomaly-detection.metric-events",
            lambda s: {"enabled": True, "summary": f"{CANARY_TAG} metric event",
                       "queryDefinition": {"type": "METRIC_KEY", "metricKey": CANARY_METRIC,
                                           "aggregation": "SUM"},
                       "modelProperties": {"type": "STATIC_THRESHOLD", "threshold": 0,
                                           "alertCondition": "ABOVE", "alertingOnMissingData": False,
                                           "violatingSamples": 1, "samples": 1, "dealertingSamples": 1},
                       "eventTemplate": {"title": f"{CANARY_TAG} metric event",
                                         "description": CANARY_TAG, "eventType": "CUSTOM_ALERT",
                                         "davisMerge": False},
                       "note": CANARY_TAG},
            f"canary metric event on {CANARY_METRIC} (opens the problem marmoset disables)",
        ),
        CanarySpec(
            "log-event", "builtin:logmonitoring.log-events",
            lambda s: {"enabled": True, "summary": f"{CANARY_TAG} log event",
                       "query": f'content="{CANARY_TAG}"',
                       "queryType": "CUSTOM", "alertAfterNumberOfOccurrences": 1,
                       "eventTemplate": {"title": f"{CANARY_TAG} log event",
                                         "eventType": "CUSTOM_ALERT", "davisMerge": False},
                       "note": CANARY_TAG},
            "canary log event on chaos-canary (the alert colobus tries to blind)",
        ),
        CanarySpec(
            "log-storage", "builtin:logmonitoring.log-storage-settings",
            lambda s: {"enabled": True, "ruleName": f"{CANARY_TAG} log storage",
                       "matcher": f'content="{CANARY_TAG}"', "sendToStorage": True,
                       "note": CANARY_TAG},
            "canary log-storage rule (the object colobus mutates)",
        ),
    ]


@dataclass
class CanaryManifest:
    """The record of what setup created, so teardown can undo exactly that."""

    path: str
    mz_scope: Optional[str] = None
    created: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "CanaryManifest":
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                return cls(path=path, mz_scope=raw.get("mz_scope"),
                           created=list(raw.get("created", [])))
            except (OSError, json.JSONDecodeError):
                pass
        return cls(path=path)

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"mz_scope": self.mz_scope, "created": self.created}, fh, indent=2)


class CanaryKit:
    """Provision / inspect / remove the canary fixtures."""

    def __init__(self, client: DynatraceClient, manifest_path: str = ".troop-canary.json",
                 specs: Optional[list[CanarySpec]] = None, dry_run: bool = True) -> None:
        self.client = client
        self.dry_run = dry_run
        self.specs = specs if specs is not None else default_specs()
        self.manifest = CanaryManifest.load(manifest_path)

    # --- provision ----------------------------------------------------------

    def setup(self) -> list[str]:
        """Create the management zone, then each canary object scoped to it."""
        msgs: list[str] = []
        tag = "[dry-run] would create" if self.dry_run else "created"

        # 1. The management zone. Its object id doubles as the canary scope.
        mz_val = _mz_value()
        if self.dry_run:
            mz_id = "DRYRUN-management-zone"
            self.client.create_object(MZ_SCHEMA, "environment", mz_val)  # record in sim store
        else:
            try:
                mz_id = self.client.create_object(MZ_SCHEMA, "environment", mz_val)
            except Exception as exc:
                return [f"[FAIL] could not create the canary management zone: {exc}",
                        "nothing else attempted (every canary object scopes to the zone)."]
        msgs.append(f"{tag} management zone {CANARY_TAG}-zone -> {mz_id}")

        created: list[dict[str, Any]] = [
            {"key": "management-zone", "schema_id": MZ_SCHEMA, "object_id": mz_id,
             "scope": "environment"}
        ]

        # 2. One canary object per event-drivable monkey, scoped to the zone.
        for spec in self.specs:
            value = spec.value(mz_id)
            if self.dry_run:
                oid = self.client.create_object(spec.schema_id, mz_id, value)
                msgs.append(f"{tag} {spec.note}")
            else:
                try:
                    oid = self.client.create_object(spec.schema_id, mz_id, value)
                    msgs.append(f"{tag} {spec.note} -> {oid}")
                except Exception as exc:
                    msgs.append(f"[skip] {spec.note}: {exc} "
                                f"(the schema may differ on your tenant - adjust canary.py)")
                    continue
            created.append({"key": spec.key, "schema_id": spec.schema_id,
                            "object_id": oid, "scope": mz_id})

        self.manifest.mz_scope = mz_id
        self.manifest.created = created
        if not self.dry_run:
            self.manifest.save()
            msgs.append(f"manifest written: {self.manifest.path}")

        msgs.append("")
        if self.dry_run:
            msgs.append("dry-run only - nothing was created. Re-run with --execute against a "
                        "live tenant (needs the settings.write scope).")
        else:
            msgs.append("canary provisioned. Add this scope to your tenant in chaos.yaml:")
            msgs.append(f"    canary_scopes: [\"{mz_id}\"]")
            msgs.append("then:  troop verify   (prove the schemas)")
            msgs.append(f"       troop doctor  (prove the fence is safe)")
            msgs.append(f"       troop run baboon --scope \"{mz_id}\" --execute --oracle-wait 600")
            msgs.append("  (baboon, ape, mandrill, howler, tamarin, marmoset, colobus are live now "
                        "- their matching detectors were provisioned too; the TRACE monkeys need "
                        "the reference canary. see `troop list`)")
        return msgs

    # --- inspect ------------------------------------------------------------

    def status(self) -> list[str]:
        if not self.manifest.created:
            return ["no canary provisioned (no manifest). Run: troop canary setup --execute"]
        msgs = [f"canary scope: {self.manifest.mz_scope}",
                f"{len(self.manifest.created)} object(s) recorded in {self.manifest.path}:"]
        for item in self.manifest.created:
            msgs.append(f"  - {item['key']:20s} {item['schema_id']}  [{item['object_id']}]")
        return msgs

    # --- remove -------------------------------------------------------------

    def teardown(self) -> list[str]:
        """Delete exactly what setup created, children first, the zone last."""
        if not self.manifest.created:
            return ["nothing to tear down (no canary manifest)."]
        msgs: list[str] = []
        tag = "[dry-run] would delete" if self.dry_run else "deleted"
        remaining: list[dict[str, Any]] = []
        for item in reversed(self.manifest.created):
            oid = item["object_id"]
            if self.dry_run:
                msgs.append(f"{tag} {item['key']} [{oid}]")
                continue
            try:
                self.client.delete_object(oid)
                msgs.append(f"{tag} {item['key']} [{oid}]")
            except Exception as exc:
                msgs.append(f"[skip] {item['key']} [{oid}]: {exc} (delete it by hand)")
                remaining.append(item)
        if not self.dry_run:
            self.manifest.created = list(reversed(remaining))
            self.manifest.mz_scope = self.manifest.mz_scope if remaining else None
            self.manifest.save()
            msgs.append("manifest cleared." if not remaining
                        else f"{len(remaining)} object(s) left in the manifest (see skips above).")
        return msgs
