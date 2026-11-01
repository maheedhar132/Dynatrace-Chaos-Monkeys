# Dynatrace API Index

The complete map of Dynatrace's API surface, seen through a chaos lens: **what each API controls, and whether the Chaos Monkey can read it, break it, or use it to inject faults.**

> **Legend**
> - **Role:** `OBSERVE` = used as the oracle (did Dynatrace notice?) · `INJECT` = used to create a known fault · `MUTATE` = a chaos target (config we break) · `GUARD` = safety (audit, snapshot, restore) · `—` = not relevant
> - **Blast:** 🟢 low (scoped, reversible) · 🟡 medium (affects alerting for a scope) · 🔴 high (can blind the whole environment or touch the monitored apps)
> - **Source:** ✅ confirmed in docs.dynatrace.com while researching · ⚠️ from prior knowledge, **verify on a tenant**
> - **Gen:** `C` = Dynatrace Classic · `L` = Latest Dynatrace (Gen3 platform, Grail) · `C+L` = both

## Hosts and authentication

| Surface | Base URL | Auth |
|---|---|---|
| Environment API (classic) | `https://{env-id}.live.dynatrace.com/api/v2/...`, `/api/v1/...` | API token (`Authorization: Api-Token dt0c01...`) |
| Configuration API (classic) | `https://{env-id}.live.dynatrace.com/api/config/v1/...` | API token |
| Via ActiveGate | `https://{activegate}:9999/e/{env-id}/api/v2/...` ✅ | API token |
| Platform APIs (Gen3) | `https://{env-id}.apps.dynatrace.com/platform/...` ✅ | Platform token or OAuth bearer ✅ |
| Classic API proxied through the platform | `https://{env-id}.apps.dynatrace.com/platform/classic/environment-api/v2/...` ✅ | Platform token or OAuth. **Required for Davis anomaly detectors** ✅ |
| Account Management | `https://api.dynatrace.com/iam/v1/accounts/{uuid}/...`, `/env/v2/accounts/{uuid}/...` ⚠️ | OAuth client (`sso.dynatrace.com`) ⚠️ |
| API Explorer (live, per tenant) | `https://{env-id}.live.dynatrace.com/rest-api-doc/` ✅ | the browser session |

> The live API Explorer and `GET /api/v2/settings/schemas` are the **authoritative index for a given tenant**. Schema sets differ between Classic, Latest, SaaS and Managed, and by version. Phase 2 of this project generates this file automatically from those two sources.

---

## 1. Environment API v2 (and surviving v1)

| Family | Key endpoints | Role | Blast | Gen | Src |
|---|---|---|---|---|---|
| **Settings 2.0** | `GET /api/v2/settings/schemas`, `GET /api/v2/settings/schemas/{id}`, `GET/POST /api/v2/settings/objects`, `GET/PUT/DELETE /api/v2/settings/objects/{objectId}`, `GET /api/v2/settings/effectiveValues` ⚠️ | **MUTATE** (primary target), GUARD (snapshot) | 🟢→🔴 per schema | C+L | ✅ families / ⚠️ paths |
| Settings: permissions and ownership | per-object permissions, transfer ownership ✅ | MUTATE (access chaos) | 🟡 | C+L | ✅ |
| **Problems v2** | `GET /api/v2/problems`, `GET /api/v2/problems/{id}`, `POST /api/v2/problems/{id}/close`, comments CRUD | **OBSERVE** (main oracle) | — | C+L | ✅ ops / ⚠️ paths |
| **Events v2** | `POST /api/v2/events/ingest` ✅, `GET /api/v2/events`, `/api/v2/eventTypes`, `/api/v2/eventProperties` | **INJECT** (synthetic `ERROR_EVENT`, `CUSTOM_ALERT`, ...), OBSERVE | 🟢 | C+L | ✅ |
| **Metrics v2** | `POST /api/v2/metrics/ingest` ✅, `GET /api/v2/metrics`, `/api/v2/metrics/query`, `DELETE /api/v2/metrics/{key}` | **INJECT** (canary metric that breaches a threshold), OBSERVE | 🟢 | C+L | ✅ |
| Metrics v1 / Timeseries v1 | `/api/v1/timeseries` | — deprecated, end of life end of 2025 ✅ | — | C | ✅ |
| Metric units | `/api/v2/units` | — | — | C+L | ⚠️ |
| Monitored entities | `GET /api/v2/entities`, `/api/v2/entityTypes`, `POST /api/v2/entities/custom`; monitoring state ✅ | OBSERVE (is the entity still seen?) | — | C+L | ✅ / ⚠️ |
| Custom tags | `GET/POST/DELETE /api/v2/tags` | MUTATE (remove the tags that routing depends on) | 🟡 | C | ✅ / ⚠️ |
| **Access tokens** | `GET/POST /api/v2/apiTokens`, `POST /api/v2/apiTokens/lookup`, `PUT/DELETE /api/v2/apiTokens/{id}` | MUTATE (revoke or expire an *integration* token) | 🔴 | C+L | ✅ family / ⚠️ paths |
| ActiveGate tokens | `/api/v2/activeGateTokens` | MUTATE | 🔴 | C+L | ✅ / ⚠️ |
| Tenant tokens | `/api/v2/tenantTokenRotation/{start,finish,cancel}` | — **never touch** (rotation can disconnect every OneAgent) | 🔴 | C+L | ✅ / ⚠️ |
| Audit logs | `GET /api/v2/auditlogs` | **GUARD** (prove what changed and who changed it) | — | C (L uses Grail audit logs ✅) | ✅ |
| Network zones | `/api/v2/networkZones`, `/api/v2/networkZoneSettings` | MUTATE (route agents to a zone with no ActiveGate) | 🔴 | C+L | ✅ / ⚠️ |
| ActiveGate | `/api/v2/activeGates`, auto-update config and jobs ✅ | OBSERVE, MUTATE (auto-update) | 🟡 | C+L | ✅ |
| OneAgent on a host | `GET /api/v1/oneagents` | OBSERVE (agent health, monitoring mode) | — | C+L | ✅ / ⚠️ |
| Remote configuration | OneAgent and ActiveGate remote config management ✅ | MUTATE (change agent host group or network zone remotely) | 🔴 | C+L | ✅ |
| Deployment | OneAgent and ActiveGate installers, BOSH, orchestration ✅ | — | — | C+L | ✅ |
| Log Monitoring v2 | `POST /api/v2/logs/ingest`; `search`/`aggregate`/`export` **deprecated** (end of life end of 2027) ✅ | INJECT (log-based alerts) | 🟢 | C (L: Grail) | ✅ |
| Business events | `POST /api/v2/bizevents/ingest` | INJECT | 🟢 | C+L | ✅ / ⚠️ |
| SLO | classic `/api/v2/slo`; platform `/platform/slo/v1/...` ✅ | MUTATE (target or evaluation window), OBSERVE | 🟡 | C / L | ✅ / ⚠️ |
| Synthetic v1/v2 | monitors, locations, nodes, executions, third-party ✅ | MUTATE (disable a monitor, move it to a dead location), INJECT (point it at a failing URL) | 🟡 | C+L | ✅ |
| RUM | geographic regions, user sessions, JavaScript code ✅ | — (injection config lives in Settings) | — | C+L | ✅ |
| Application Security | vulnerabilities, Davis security advice, attacks ✅ | OBSERVE | — | C+L | ✅ |
| Credential vault | `/api/v2/credentials` | MUTATE (break the credentials synthetic monitors and extensions use) | 🟡 | C+L | ✅ / ⚠️ |
| Extensions 2.0 | `/api/v2/extensions/...` → being replaced by the Extensions Platform API (deprecated SaaS 1.342) ✅ | MUTATE (disable a monitoring configuration) | 🟡 | C+L | ✅ |
| Anonymization | anonymization jobs ✅ | — **never touch** (deletes data) | 🔴 | C+L | ✅ |
| Releases | `/api/v2/releases` | — | — | C+L | ✅ |
| Hub capabilities | ✅ | — | — | C+L | ✅ |
| Cluster information | `/api/v1/config/clusterversion` | OBSERVE (detect version and generation) | — | C+L | ✅ / ⚠️ |

## 2. Configuration API v1 (`/api/config/v1/...`): legacy

Most of these resources have **moved to Settings 2.0**, so treat this API as read-only legacy. The Chaos Monkey targets the Settings equivalents instead.

| Area | Endpoints (examples) | Settings 2.0 replacement |
|---|---|---|
| Anomaly detection | `anomalyDetection/{applications,services,hosts,databaseServices,diskEvents,processGroups,aws,vmware,metricEvents}` ✅ | `builtin:anomaly-detection.*` → in Latest, `builtin:davis.anomaly-detectors` ✅ |
| Alerting profiles | `alertingProfiles` ⚠️ | `builtin:alerting.profile` → in Latest, **Workflows** ✅ |
| Problem notifications | `notifications` ⚠️ | `builtin:problem.notifications` → in Latest, **Workflows** ✅ |
| Maintenance windows | `maintenanceWindows` ✅ (migration guide) | `builtin:alerting.maintenance-window` / `builtin:maintenance-windows` ✅ |
| Management zones | `managementZones` ⚠️ | `builtin:management-zones` → in Latest, **Segments + IAM policies** ✅ |
| Auto tags | `autoTags` ⚠️ | `builtin:tags.auto-tagging` → **removed** in Latest ✅ |
| Service config | custom services, failure detection, request attributes, request naming, detection rules ✅ | `builtin:service-detection-rules`, `builtin:failure-detection.*` ⚠️ |
| OneAgent config | on host, host group, environment-wide ✅ | `builtin:host.monitoring.mode`, `builtin:oneagent.features` ✅ |
| Calculated metrics | service, RUM, mobile, synthetic ✅ | Latest: OpenPipeline metric extraction ✅ |
| Conditional naming, data privacy, dashboards, reports, plugins, AWS/Azure credentials, remote environments, RUM app config ✅ | — | various |

## 3. Platform APIs (Latest Dynatrace / Gen3): `{env}.apps.dynatrace.com/platform/...`

| Service | Purpose | Role | Blast | Src |
|---|---|---|---|---|
| **Settings service** (`/platform/classic/environment-api/v2/settings/...`) | Settings 2.0 through platform auth, needed for Davis anomaly detectors ✅ | **MUTATE** | 🟢→🔴 | ✅ |
| **AutomationEngine** (`/platform/automation/v1/workflows`, `/executions` ⚠️) | Workflows: problem trigger, Davis event trigger, event (DQL matcher) trigger ✅. **Replaces alerting profiles and notifications in Latest.** | **MUTATE** (trigger filter, severity, disable), OBSERVE (did an execution run?) | 🟡 | ✅ / ⚠️ |
| **Grail service** (`/platform/storage/query/v1/query:execute` ⚠️) | DQL queries over logs, metrics, events, spans, bizevents | **OBSERVE** (query `dt.davis.problems`, `dt.davis.events`) | — | ✅ / ⚠️ |
| Grail storage management (`/platform/storage/management/v1/bucket-definitions` ⚠️) | Buckets and retention | — **never touch** (data loss) | 🔴 | ⚠️ |
| **OpenPipeline** | Ingest sources, routing, pipelines (drop, mask, extract). Legacy `/platform/openpipeline/v1/configurations` is deprecated (end of life 2026-06-29) → Settings schemas `builtin:openpipeline.<scope>.{routing,pipelines,ingest-sources}` ✅ | **MUTATE** (add a drop processor or misroute to a pipeline) | 🔴 | ✅ |
| Dynatrace Intelligence service | AI/ML (Davis analyzers) ✅ | OBSERVE | — | ✅ |
| Document service (`/platform/document/v1/documents` ⚠️) | Dashboards and notebooks (Latest) | MUTATE (low value) | 🟢 | ✅ / ⚠️ |
| AppEngine (registry, functions) | Custom apps ✅ | — | — | ✅ |
| App settings, State, Notification (email) services ✅ | app-scoped storage and notifications | — / OBSERVE (email notifications) | — | ✅ |
| Platform management service | license and environment info ✅ | OBSERVE (detect generation and capabilities) | — | ✅ |
| Classic environment service | proxies classic APIs into AppEngine and Workflows ✅ | — | — | ✅ |

## 4. Account Management API

| Group | Role | Blast |
|---|---|---|
| Users, groups, permissions, **policies (IAM)** ✅ | MUTATE: remove the permission a workflow's actor or a service user needs → automation silently fails | 🔴 |
| Service users, platform tokens ✅ | MUTATE: revoke the identity a workflow runs as | 🔴 |
| Environment management, subscription (DPS), account limits, account audit logs, notifications, reference data ✅ | OBSERVE / GUARD | — |

## 5. Out of scope

- **Cluster API (Managed only).** Infrastructure-level changes, never targeted.
- **Anonymization, Grail bucket deletion, tenant token rotation, OneAgent uninstall.** Irreversible. These are on the hard denylist.

## Token scopes the Chaos Monkey needs (minimum)

| Purpose | Classic API token | Platform/OAuth |
|---|---|---|
| Read and mutate settings | `settings.read`, `settings.write` | `settings:schemas:read`, `settings:objects:read`, `settings:objects:write` ✅ |
| Inject events | `events.ingest` ✅ | `openpipeline:events.davis:ingest` ✅ |
| Inject metrics | `metrics.ingest` ✅ | `openpipeline:metrics:ingest` ✅ |
| Observe problems | `problems.read` ⚠️ | `storage:events:read` ✅, `storage:metrics:read` ✅ |
| Observe workflows | — | `automation:workflows:read` ⚠️ |
| Run Davis anomaly detectors as a service user | — | `iam:service-users:use`, `davis:analyzers:execute` ✅ |
| Audit | `auditLogs.read` ⚠️ | — |
