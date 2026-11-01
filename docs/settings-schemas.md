# Settings 2.0 schemas: the chaos target list

Settings 2.0 is where nearly all Dynatrace behaviour is configured, so it is the Chaos Monkey's main attack surface. Every setting is an **object** that belongs to a **schema** (`builtin:...`) and a **scope** (environment → host group → host / entity). Lower scopes override higher ones.

> **Classic vs Latest matters.** Many classic schemas are **hidden in Latest Dynatrace** and replaced by platform features ([removed-schemas reference](https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings/removed-schemas)). The Chaos Monkey must detect the generation first and pick the right target set.
>
> Status: **Live** = available in both · **Classic-only** = hidden in Latest (the replacement is shown) · ⚠️ = verify on a tenant.

Schema IDs come from the [schema list](https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings/schemas). Get the authoritative per-tenant list with `GET /api/v2/settings/schemas`.

---

## Stage 1: Ingest (does data arrive at all?)

| Schema | Controls | Chaos mutation | Status | Blast |
|---|---|---|---|---|
| `builtin:host.monitoring.mode` | Full-stack / infrastructure / discovery mode per host | Full-stack → infrastructure (traces, services and response-time detection disappear) | Live | 🔴 |
| `builtin:oneagent.features` | Per-technology code-module features | Turn off a technology sensor (for example Java or Node.js) | Live | 🔴 |
| `builtin:deployment.oneagent.default-mode` | Default mode for new hosts | New hosts join in a lower mode | Live | 🟡 |
| `builtin:deployment.oneagent.updates` / `builtin:deployment.activegate.updates` | Auto-update | Freeze updates (slow drift) | Live | 🟢 |
| `builtin:process.process-monitoring`, `builtin:process.custom-process-monitoring-rule`, `builtin:process.built-in-process-monitoring-rule` | Which processes get deep monitoring | Add an exclusion rule for the target process | Live | 🔴 |
| `builtin:process-group.monitoring.state`, `builtin:host.process-groups.monitoring-state` | Monitoring on or off per process group | Disable monitoring for a process group | Live | 🟡 |
| `builtin:networkzones`, `builtin:networkzones.zones` | Agent → ActiveGate routing | Assign the zone to a non-existent ActiveGate | Live | 🔴 |
| `builtin:logmonitoring.log-storage-settings` | Log ingest rules (include / exclude) | Exclude the source the log alert depends on | Live ⚠️ | 🟡 |
| `builtin:logmonitoring.log-agent-configuration`, `builtin:logmonitoring.custom-log-source-settings` | Log agent and sources | Remove a custom log source | Live ⚠️ | 🟡 |
| `builtin:openpipeline.<scope>.{ingest-sources,routing,pipelines}` | OpenPipeline (Latest) | Add a **drop** processor that matches the canary, or route to a pipeline with no storage | Live (Latest) | 🔴 |
| `builtin:trace.ingest.control`, `builtin:global.trace.ingest.control`, `builtin:rum.overload-prevention` | Ingest throttling | Lower the caps until data is sampled away | Live | 🟡 |
| `builtin:rum.web.enablement`, `builtin:rum.mobile.enablement`, `builtin:rum.custom.enablement` | RUM on or off | Disable RUM for the app | Live | 🟡 |
| `builtin:logmonitoring.log-dpp-rules` | Log processing (Classic) | Drop or rename the alerting field | Classic-only → OpenPipeline | 🟡 |

## Stage 2: Entity model (is the data attached to the right thing?)

| Schema | Controls | Chaos mutation | Status | Blast |
|---|---|---|---|---|
| `builtin:process-group.simple-detection-rule`, `builtin:process-group.advanced-detection-rule`, `builtin:process-grouping-rules`, `builtin:declarativegrouping` | Process group identity | Split or merge PGs → baselines reset, alerts land on a "new" entity | Live | 🟡 |
| `builtin:process-group.detection-flags` | PG detection behaviour | Flip a flag (for example container grouping) | Live | 🟡 |
| `builtin:service-detection-rules`, `builtin:service-detection-v2-for-oneagent`, `builtin:service-splitting-rules` | Service identity | Split a service → its anomaly baseline restarts from zero | Live | 🟡 |
| `builtin:service-detection.full-web-request`, `builtin:service-detection.full-web-service` | Web service detection | Change the rules | Live | 🟡 |
| `builtin:tags.auto-tagging` | Auto-tags (used by routing everywhere in Classic) | Invert a condition → the entity loses the `team:` tag → wrong or no routing | Classic-only (removed) | 🟡 |
| `builtin:management-zones` | MZ membership (filters alerting profiles and dashboards) | Narrow a rule so the entity falls out of the MZ | Classic-only → Segments + IAM | 🟡 |
| `builtin:naming.hosts`, `builtin:naming.processes-and-containers`, `builtin:naming.services` | Display names | Rename → breaks name-based filters | Classic-only | 🟢 |

## Stage 3: Detection (does Davis notice?)

| Schema | Controls | Chaos mutation | Status | Blast |
|---|---|---|---|---|
| `builtin:davis.anomaly-detectors` | **Latest:** DQL-based custom detectors (static / auto-adaptive / seasonal) ✅ | `enabled=false`; threshold ×10; `alertCondition` flip; `violatingSamples` raised to 60 | Live (platform auth only) | 🟢 per detector |
| `builtin:anomaly-detection.metric-events` | **Classic:** metric events | Same mutations | Classic-only → anomaly detectors | 🟢 |
| `builtin:anomaly-detection.services` | Service response time, failure rate, load | Raise thresholds / switch to "don't detect" | Live | 🟡 |
| `builtin:anomaly-detection.infrastructure-hosts` | CPU, memory, disk, connectivity | Disable a detector | Live | 🟡 |
| `builtin:anomaly-detection.rum-web`, `builtin:anomaly-detection.rum-mobile` | Frontend anomalies | Raise thresholds | Live | 🟡 |
| `builtin:anomaly-detection.kubernetes.{cluster,namespace,node,pvc,workload}` | K8s anomalies | Disable a detector | Live | 🟡 |
| `builtin:anomaly-detection.holiday-aware-baseline` | Baseline behaviour | Toggle | Live ⚠️ | 🟢 |
| `builtin:anomaly-detection.frequent-issues` | Frequent-issue suppression | Turn suppression on → a recurring real issue gets muted | Classic-only (removed) | 🟡 |
| `builtin:infrastructure.disk.edge.anomaly-detectors` | Disk detectors (Latest) | Disable | Live (Latest) | 🟡 |
| `builtin:anomaly-detection.disk-rules`, `builtin:anomaly-detection.infrastructure-disks` | Disk detectors (Classic) | Disable | Classic-only | 🟡 |
| `builtin:availability.process-group-alerting` | "Process unavailable" alerts | Disable for the PG | Classic-only → health alerts | 🟡 |
| `builtin:alerting.connectivity-alerts` | Host connectivity alerts | Disable | Live ⚠️ | 🟡 |
| `builtin:failure-detection.*` | What counts as a failed request ⚠️ | Mark 5xx as success → failure-rate alerts never fire | Live ⚠️ | 🟡 |

## Stage 4: Alerting and notification (does a human find out?)

| Schema / feature | Controls | Chaos mutation | Status | Blast |
|---|---|---|---|---|
| `builtin:alerting.profile` | Severity filters, delays, MZ / tag filters | Raise the minimum severity; add a 60-min delay; add an exclusion tag | Classic-only → **Workflows** | 🟡 |
| `builtin:problem.notifications` | Webhook / Slack / PagerDuty / email / Jira integrations | `enabled=false`; corrupt the webhook URL; switch the alerting profile | Classic-only → **Workflows** | 🟡 |
| **Workflows** (AutomationEngine API, not Settings) | Problem / Davis event / DQL triggers → notify or remediate | Raise the trigger's minimum severity; break the DQL matcher; disable the workflow; revoke the actor's permission | Latest | 🟡 |
| `builtin:alerting.maintenance-window`, `builtin:maintenance-windows` | Suppression windows | Insert an active window over the scope ("forgot to end the maintenance") | Classic schema replaced by new maintenance windows in Latest ✅ | 🟡 |
| `builtin:issue-tracking.integration` | Issue tracker link | Break the credentials | Live ⚠️ | 🟢 |
| `builtin:appsec.notification-*` | Security notifications | Disable | Live ⚠️ | 🟢 |

## Stage 5: Access and analysis

| Schema / API | Controls | Chaos mutation | Status | Blast |
|---|---|---|---|---|
| `builtin:monitoring.slo` / platform SLO API | SLO definitions | Widen the target or change the metric | Classic-only → platform SLOs | 🟢 |
| API tokens (Tokens API) | Integration auth | Expire a canary integration token | Live | 🔴 if real |
| IAM policies (Account API) | Who and what may act | Remove the workflow actor's `storage:events:read` | Latest | 🔴 |

## Hard denylist (never mutated, under any flag)

Tenant token rotation · anonymization jobs · Grail bucket definitions and retention · OneAgent uninstall or deployment · `builtin:preferences.privacy` and other data-privacy / masking schemas (breaking these leaks PII rather than blinding monitoring) · account users and billing.
