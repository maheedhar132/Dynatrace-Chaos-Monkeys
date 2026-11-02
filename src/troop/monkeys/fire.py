"""The monkeys that actually run: reversible, canary-scoped experiments.

Each builds a `Plan` whose every `Change` carries a `before` snapshot (or a
`before=None` for a create, which the journal undoes by delete). None of them
mutate anything themselves — they only describe. The runner applies the plan,
and only after the guard approves it.
"""

from __future__ import annotations

import copy

from ..client import DynatraceClient
from ..models import Blast, Change, Fault, Plan, Reversibility, Target
from .base import Monkey

CANARY_TAG = "chaos-canary"


def _error_event(scope: str, title: str) -> dict:
    """A known fault: a canary error event, tagged so it's never mistaken for real."""
    return {
        "eventType": "ERROR_EVENT",
        "title": title,
        "entitySelector": None,
        "properties": {"chaos": "true", "chaos.scope": scope},
    }


def _canary_updates(client, schema_id, scope, mutate, note, label):
    """Shared pattern: find canary-tagged objects in `scope`, snapshot each, and
    build an update `Change` whose `after` is `mutate(deepcopy(value))`.

    `mutate` edits the object's `value` dict in place. Only objects whose value
    carries CANARY_TAG are eligible — belt-and-braces on top of the guard fence.
    """
    changes: list[Change] = []
    for o in client.list_objects(schema_id, scope):
        if CANARY_TAG not in str(o.get("value", {})):
            continue
        before = copy.deepcopy(o)
        after = copy.deepcopy(o)
        mutate(after.setdefault("value", {}))
        changes.append(Change(
            target=Target(schema_id, o["objectId"], scope, label=f"{CANARY_TAG} {label}"),
            before=before, after=after, note=note,
        ))
    return changes


class Marmoset(Monkey):
    """Smallest monkey. Disables the canary metric event so a metric spike stops
    opening a problem.

    Mutation: disable the metric event watching `chaos.canary.fault`. Fault: the
    driver pushes that metric (above threshold). Kill = no problem opens. This is
    the metric event `canary setup` provisions, so the baseline genuinely fires.
    """

    name = "marmoset"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.METRIC     # the driver pushes a canary metric that crosses the metric event
    schemas = ("builtin:anomaly-detection.metric-events",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        objs = client.list_objects("builtin:anomaly-detection.metric-events", scope)
        changes: list[Change] = []
        for o in objs:
            if CANARY_TAG not in str(o.get("value", {})):
                continue  # only touch metric events we marked as canaries
            before = copy.deepcopy(o)
            after = copy.deepcopy(o)
            after.setdefault("value", {})["enabled"] = False
            changes.append(Change(
                target=Target("builtin:anomaly-detection.metric-events", o["objectId"], scope,
                               label=f"{CANARY_TAG} metric event"),
                before=before, after=after, note="disable canary metric event",
            ))
        return Plan(
            monkey=self.name,
            description="Disable the canary metric event on chaos.canary.fault",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Driver pushes chaos.canary.fault above threshold",
            expected_signal="A metric-event problem opens",
        )


class Baboon(Monkey):
    """Mis-scopes a canary alerting profile so problems stop routing.

    Mutation: raise min severity to AVAILABILITY and add an exclusion tag.
    Fault: a canary ERROR_EVENT. Kill = no notification fires.
    """

    name = "baboon"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.EVENT      # an ingested ERROR_EVENT opens a problem that routes here
    schemas = ("builtin:alerting.profile",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        objs = client.list_objects("builtin:alerting.profile", scope)
        changes: list[Change] = []
        for o in objs:
            if CANARY_TAG not in str(o.get("value", {})):
                continue
            before = copy.deepcopy(o)
            after = copy.deepcopy(o)
            v = after.setdefault("value", {})
            v["severityRules"] = []            # nothing meets the filter now
            v["minProblemDuration"] = 60       # and delay it an hour
            changes.append(Change(
                target=Target("builtin:alerting.profile", o["objectId"], scope,
                               label=f"{CANARY_TAG} profile"),
                before=before, after=after, note="blind the canary alerting profile",
            ))
        return Plan(
            monkey=self.name,
            description="Raise severity filter + delay on a canary alerting profile",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Canary ERROR_EVENT", expected_signal="Routed notification within SLA",
        )


class Ape(Monkey):
    """Disables (or deletes) a canary problem notification.

    Delete is reversible here because the journal snapshots the object first and
    recreates it from `before` on restore.
    """

    name = "ape"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.EVENT      # the problem from an ingested event drives the notification
    schemas = ("builtin:problem.notifications",)

    def __init__(self, delete: bool = False) -> None:
        self.delete = delete

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        objs = client.list_objects("builtin:problem.notifications", scope)
        changes: list[Change] = []
        for o in objs:
            if CANARY_TAG not in str(o.get("value", {})):
                continue
            before = copy.deepcopy(o)
            if self.delete:
                after = None  # journal restores by recreating `before`
                note = "delete canary notification"
            else:
                after = copy.deepcopy(o)
                after.setdefault("value", {})["enabled"] = False
                note = "disable canary notification"
            changes.append(Change(
                target=Target("builtin:problem.notifications", o["objectId"], scope,
                               label=f"{CANARY_TAG} notification"),
                before=before, after=after, note=note,
            ))
        return Plan(
            monkey=self.name,
            description=("Delete" if self.delete else "Disable") + " a canary problem notification",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Canary ERROR_EVENT", expected_signal="Sink receives the webhook payload",
        )


class Mandrill(Monkey):
    """Leaves a short, TTL'd maintenance window active over the canary scope.

    Models the classic 'forgot to end the maintenance window' outage — but only
    over the canary scope, and the runner's TTL tears it down.
    """

    name = "mandrill"
    reversibility = Reversibility.AUTO
    blast = Blast.SCOPE
    fault = Fault.EVENT      # the window suppresses the problem the injected event opens
    schemas = ("builtin:alerting.maintenance-window",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        value = {
            "enabled": True,
            "generalProperties": {
                "name": f"{CANARY_TAG} window (auto)",
                "description": "chaos experiment; auto-removed",
                "maintenanceType": "PLANNED", "suppression": "DETECT_PROBLEMS_DONT_ALERT",
            },
            "scope": {"filters": [{"type": "MANAGEMENT_ZONE", "mzId": scope}]},
        }
        change = Change(
            target=Target("builtin:alerting.maintenance-window", None, scope,
                          label=f"{CANARY_TAG} window"),
            before=None, after={"value": value}, note="create suppression window",
        )
        return Plan(
            monkey=self.name,
            description="Insert an active maintenance window over the canary scope",
            reversibility=self.reversibility, blast=self.blast, changes=[change],
            fault="Canary ERROR_EVENT", expected_signal="Problem + notification (should be suppressed)",
        )


class Howler(Monkey):
    """Creates a clearly-tagged canary alerting profile + sink, scoped to the canary MZ.

    This is the *safe* version of 'alerting noise': it tests that a new profile
    routes, in an isolated zone, and the runner removes it. It is NOT the
    unfiltered tenant-wide spam described in the quarantine (see Locust).
    """

    name = "howler"
    reversibility = Reversibility.AUTO
    blast = Blast.SCOPE
    fault = Fault.EVENT      # tests that a new profile routes the injected event's problem
    schemas = ("builtin:alerting.profile",)

    def __init__(self, sink_url: str = "https://example.invalid/chaos-sink") -> None:
        self.sink_url = sink_url

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        profile = {
            "value": {
                "name": f"{CANARY_TAG} profile (auto)",
                "managementZone": scope,
                "severityRules": [{"severityLevel": "MONITORING_UNAVAILABLE"}],
                "note": CANARY_TAG,
            }
        }
        change = Change(
            target=Target("builtin:alerting.profile", None, scope, label=f"{CANARY_TAG} profile"),
            before=None, after=profile, note="create canary routing profile",
        )
        return Plan(
            monkey=self.name,
            description="Create a canary alerting profile scoped to the canary MZ",
            reversibility=self.reversibility, blast=self.blast, changes=[change],
            fault="Canary ERROR_EVENT in the canary MZ",
            expected_signal="Sink receives exactly one routed alert",
        )


class Capuchin(Monkey):
    """Tag drift. Clears a canary auto-tag rule so the entity loses its tag and
    every tag-filtered alerting route silently stops matching it.

    Mutation: empty the rule set. Fault: canary ERROR_EVENT. Kill = routed
    notification never arrives because the `team:` tag is gone.
    """

    name = "capuchin"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.TRACE      # needs a real tagged entity for the tag-filtered route to apply
    schemas = ("builtin:tags.auto-tagging",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        changes = _canary_updates(
            client, "builtin:tags.auto-tagging", scope,
            mutate=lambda v: v.update({"rules": []}),
            note="clear canary auto-tag rule", label="auto-tag",
        )
        return Plan(
            monkey=self.name,
            description="Clear a canary auto-tag rule (entity loses its routing tag)",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Canary ERROR_EVENT on the formerly-tagged entity",
            expected_signal="Tag-filtered notification routes correctly",
        )


class Gibbon(Monkey):
    """Raises service anomaly-detection thresholds so real degradation slips under
    the bar.

    Mutation: switch response-time / failure-rate / load detection to "don't
    detect". Fault: load or errors against the canary service.
    """

    name = "gibbon"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.TRACE      # needs real load/errors against the canary service
    schemas = ("builtin:anomaly-detection.services",)

    def _blind(self, v: dict) -> None:
        # Representative shape; verify exact keys per tenant schema version.
        for key in ("responseTimeDegradation", "failureRateIncrease", "loadDrops"):
            v.setdefault(key, {})["detectionMode"] = "DONT_DETECT"

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        changes = _canary_updates(
            client, "builtin:anomaly-detection.services", scope,
            mutate=self._blind, note="disable canary service detectors", label="service AD",
        )
        return Plan(
            monkey=self.name,
            description="Set a canary service's anomaly detectors to DONT_DETECT",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Load generator / error burst against the canary service",
            expected_signal="Slowdown or failure-rate problem opens",
        )


class Langur(Monkey):
    """Management-zone drift. Narrows a canary MZ rule so the entity drops out of
    the zone the alerting profile filters on. (The monkey you called 'Lagoon'.)

    Mutation: replace the rule set with one that matches nothing. Fault: canary
    ERROR_EVENT. Kill = MZ-filtered notification misses it.
    """

    name = "langur"
    reversibility = Reversibility.AUTO
    blast = Blast.SCOPE
    fault = Fault.TRACE      # needs a real entity in the zone for MZ-filtered routing to apply
    schemas = ("builtin:management-zones",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        def narrow(v: dict) -> None:
            v["rules"] = [{"type": "ME", "enabled": True, "conditions": [
                {"key": {"attribute": "HOST_NAME"}, "comparisonInfo": {
                    "type": "STRING", "operator": "EQUALS",
                    "value": "__chaos_never_matches__", "caseSensitive": True}}]}]
        changes = _canary_updates(
            client, "builtin:management-zones", scope,
            mutate=narrow, note="narrow canary MZ to match nothing", label="management-zone",
        )
        return Plan(
            monkey=self.name,
            description="Narrow a canary management zone so the entity falls out of it",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Canary ERROR_EVENT", expected_signal="MZ-filtered notification routes",
        )


class Colobus(Monkey):
    """Log-ingest exclusion. Adds an exclude rule so the canary log source never
    reaches storage, and log-based alerts on it go quiet.

    Mutation: append an exclude rule. Fault: a canary log line.
    """

    name = "colobus"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.LOG        # the driver pushes a canary log line into the pipeline
    schemas = ("builtin:logmonitoring.log-storage-settings",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        def exclude(v: dict) -> None:
            v.setdefault("rules", []).append(
                {"enabled": True, "sendToStorage": False,
                 "matcher": 'content="chaos-canary"', "note": CANARY_TAG})
        changes = _canary_updates(
            client, "builtin:logmonitoring.log-storage-settings", scope,
            mutate=exclude, note="exclude canary log source from storage", label="log-storage",
        )
        return Plan(
            monkey=self.name,
            description="Exclude the canary log source from storage",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Canary log line (content=chaos-canary)",
            expected_signal="Log-based alert fires on the canary source",
        )


class Saki(Monkey):
    """Monitoring-mode downgrade on a single canary host: full-stack to
    infrastructure, so services and traces disappear while the host stays green.

    This is the troop's one MANUAL-reversibility fire monkey: the setting PUTs
    back cleanly, but the host may need a process restart to deep-monitor again
    — so it is allowed only on an explicitly allow-listed canary host scope
    (HOST-xxxx), never a management zone or the environment.
    """

    name = "saki"
    reversibility = Reversibility.MANUAL
    blast = Blast.OBJECT
    fault = Fault.TRACE      # needs a real service erroring on the downgraded host
    schemas = ("builtin:host.monitoring.mode",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        changes: list[Change] = []
        for o in client.list_objects("builtin:host.monitoring.mode", scope):
            before = copy.deepcopy(o)
            after = copy.deepcopy(o)
            after.setdefault("value", {})["monitoringMode"] = "INFRASTRUCTURE"
            changes.append(Change(
                target=Target("builtin:host.monitoring.mode", o["objectId"], scope,
                              label=f"{CANARY_TAG} host mode"),
                before=before, after=after, note="downgrade canary host to infrastructure mode",
            ))
        return Plan(
            monkey=self.name,
            description="Downgrade a canary host from full-stack to infrastructure mode",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Error on a service running on the canary host",
            expected_signal="Service problem opens for that host",
        )


class Mangabey(Monkey):
    """Failure-detection drift (best-practice gap #10). Redefines a canary
    failure-detection rule so server-side errors no longer count as failures —
    the service's failure rate then reads ~0% while it is actually erroring.

    Mutation: stop counting 5xx as failures. Fault: canary service returns 5xx.
    Kill = no failure-rate problem opens.

    NOTE: the exact value keys vary by schema version; verify on a tenant.
    """

    name = "mangabey"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.TRACE      # needs the canary service to actually return 5xx
    schemas = ("builtin:failure-detection.environment.rules",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        def ignore_5xx(v: dict) -> None:
            v.setdefault("httpResponseCodes", {}).update(
                {"serverSideErrorCodes": {"considerAsFailure": False},
                 "clientSideErrorCodes": {"considerAsFailure": False}})
        changes = _canary_updates(
            client, "builtin:failure-detection.environment.rules", scope,
            mutate=ignore_5xx, note="stop counting canary 5xx as failures",
            label="failure-detection",
        )
        return Plan(
            monkey=self.name,
            description="Redefine a canary failure-detection rule so 5xx count as success",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Canary service returns HTTP 5xx",
            expected_signal="A failure-rate increase problem opens",
        )


class Gelada(Monkey):
    """SLO goalpost move (best-practice gap #11). Lowers a canary SLO's target so
    it reads healthy no matter how badly the service behaves.

    Mutation: target/warning -> 0. Fault: canary error burst. Kill = SLO stays
    green / no burn-rate alert.

    NOTE: targets a classic `builtin:monitoring.slo` object; on Latest the SLO
    lives behind the platform SLO API (a future client extension).
    """

    name = "gelada"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.TRACE      # needs a real error burst to move the SLO
    schemas = ("builtin:monitoring.slo",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        def widen(v: dict) -> None:
            v["target"] = 0.0
            v["warning"] = 0.0
        changes = _canary_updates(
            client, "builtin:monitoring.slo", scope,
            mutate=widen, note="lower canary SLO target to 0", label="SLO",
        )
        return Plan(
            monkey=self.name,
            description="Lower a canary SLO's target so it can never breach",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Canary error burst against the SLO's service",
            expected_signal="SLO status degrades / burn-rate alert fires",
        )


class Tamarin(Monkey):
    """Frequent-issue muting (best-practice gap #14). Turns on frequent-issue
    detection for the canary so a recurring problem is folded away as 'known'
    and stops alerting.

    Mutation: enable frequent-issue detection flags. Fault: repeat the canary
    ERROR_EVENT several times. Kill = later occurrences raise no fresh alert.

    NOTE: `builtin:anomaly-detection.frequent-issues` is Classic; on Latest this
    behaviour moves into Davis problem-merge settings.
    """

    name = "tamarin"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.EVENT      # the driver repeats the event so the recurrence is real
    schemas = ("builtin:anomaly-detection.frequent-issues",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        def suppress(v: dict) -> None:
            for key in ("detectApps", "detectTxnsAndServices", "detectInfra"):
                v[key] = True   # fold recurring issues into 'frequent' -> muted
        changes = _canary_updates(
            client, "builtin:anomaly-detection.frequent-issues", scope,
            mutate=suppress, note="enable frequent-issue muting for the canary",
            label="frequent-issues",
        )
        return Plan(
            monkey=self.name,
            description="Enable frequent-issue detection so a recurring canary problem is muted",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="Repeat the canary ERROR_EVENT several times",
            expected_signal="Each recurrence still raises/updates a problem",
        )


class Loris(Monkey):
    """Auto-update freeze (best-practice gap #13). Disables OneAgent auto-update
    for the canary host group, so agents drift onto stale versions — a slow,
    silent erosion of monitoring fidelity.

    Mutation: turn auto-update off. Fault: none to inject; the 'signal' is that
    version currency / an out-of-date alert should flag the frozen group.

    NOTE: `builtin:deployment.oneagent.updates` shape varies by version; the
    ActiveGate equivalent is `builtin:deployment.activegate.updates`.
    """

    name = "loris"
    reversibility = Reversibility.AUTO
    blast = Blast.SCOPE
    fault = Fault.NONE       # the fault is agent version drift over time; nothing to inject
    schemas = ("builtin:deployment.oneagent.updates",)

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        changes = _canary_updates(
            client, "builtin:deployment.oneagent.updates", scope,
            mutate=lambda v: v.update({"enabled": False, "autoUpdate": "DISABLED"}),
            note="freeze OneAgent auto-update for the canary group", label="oneagent-updates",
        )
        return Plan(
            monkey=self.name,
            description="Freeze OneAgent auto-update for the canary host group",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="(none — drift fault) agents fall behind the current version",
            expected_signal="Version-currency / out-of-date detection flags the group",
        )


class Tarsier(Monkey):
    """Token death (best-practice gap #12). Revokes a dedicated canary ingest
    token; whatever pushed with it now gets 401s and its data silently stops.

    Tests least-privilege + whether token death is noticed. The fault (the sender
    keeps pushing and failing) happens outside Dynatrace, so the expected signal
    is a missing-data / no-metric alert on the canary source.

    Scope: tokens aren't management-zone scoped, so this targets the pseudo-scope
    `canary-token`, which the operator must add to `canary_scopes` to arm it. Only
    tokens whose name/scopes carry the canary tag are eligible.
    """

    name = "tarsier"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.NONE       # the 401s happen in the sender outside Dynatrace
    schemas = ("api-token",)  # not a Settings 2.0 schema; verified via the tokens API

    def __init__(self, token_scope: str = "canary-token") -> None:
        self.token_scope = token_scope

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        changes: list[Change] = []
        for t in getattr(client, "list_tokens", lambda: [])():
            if CANARY_TAG not in (str(t.get("name", "")) + str(t.get("scopes", ""))):
                continue
            changes.append(Change(
                target=Target("api-token", t["id"], self.token_scope,
                              label=f"{CANARY_TAG} token", kind="token"),
                before={"name": t.get("name"), "scopes": t.get("scopes", [])},
                after=None, note="revoke canary ingest token",
            ))
        return Plan(
            monkey=self.name,
            description="Revoke a dedicated canary ingest token",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="The integration keeps pushing with the revoked token (sender gets 401)",
            expected_signal="A missing-data / no-metric alert fires for the canary source",
        )


class Vervet(Monkey):
    """Synthetic blindness (best-practice gap #15). Disables a canary synthetic
    monitor, so an endpoint failure it was meant to catch goes unseen.

    Scope: synthetic monitors aren't MZ-scoped either, so this targets the
    pseudo-scope `canary-synthetic` (add it to `canary_scopes` to arm). Only
    monitors whose config carries the canary tag are eligible. The whole monitor
    is snapshotted so the revert PUTs it back intact.
    """

    name = "vervet"
    reversibility = Reversibility.AUTO
    blast = Blast.OBJECT
    fault = Fault.NONE       # the synthetic monitor must actually run against its target
    schemas = ("synthetic-monitor",)  # not a Settings 2.0 schema; verified via the synthetic API

    def __init__(self, monitor_scope: str = "canary-synthetic") -> None:
        self.monitor_scope = monitor_scope

    def plan(self, client: DynatraceClient, scope: str) -> Plan:
        changes: list[Change] = []
        for m in getattr(client, "list_monitors", lambda: [])():
            if CANARY_TAG not in str(m):
                continue
            mid = m.get("entityId") or m.get("id")
            changes.append(Change(
                target=Target("synthetic-monitor", mid, self.monitor_scope,
                              label=f"{CANARY_TAG} monitor", kind="synthetic"),
                before={"value": m},
                after={"value": {**m, "enabled": False}},
                note="disable canary synthetic monitor",
            ))
        return Plan(
            monkey=self.name,
            description="Disable a canary synthetic monitor",
            reversibility=self.reversibility, blast=self.blast, changes=changes,
            fault="The canary monitor's target URL starts returning 500",
            expected_signal="A synthetic availability problem opens",
        )
