# Monkey lineup — best-practice gap analysis

Written from the point of view of a Dynatrace engineer: *given how Dynatrace
actually tracks changes and what it recommends, which best practices does the
current troop not yet test, and what monkeys fill the gaps?*

---

## A. Is every change tracked? (Short answer: no — and "tracked" ≠ "noticed")

Dynatrace has **three separate audit logs**, and none of them is the safety net people assume it is.

| Audit log | Records | Retention | Default | Access |
|---|---|---|---|---|
| **Grail audit logs** (Latest) | Environment **settings changes** + API gateway requests | **1 year** | on, can't be turned off | DQL on `dt.system.events` (`event.kind=="AUDIT_EVENT"`) |
| **Classic environment audit log** | Logins/logouts, config changes, **API token changes** | ~**30 days** | **off by default** (Settings → Preferences) | `GET /api/v2/auditlogs` |
| **Account Management audit log** | IAM / account changes, budget & cost settings | **10 years** | on | Account Mgmt → Audit log / API |

**What is *not* tracked — the blind spots that matter for us:**

1. **Platform-service actions aren't recorded yet** — e.g. an app installed by AppEngine, a document/dashboard shared. A workflow or app doing damage can be invisible to the audit log.
2. **Ingested data is never an audit event** — only *changes to ingest config* are. So OpenPipeline dropping your logs shows up as a config change, but the missing data does not.
3. **Token *usage* isn't audited** — only token *creation/change*. A leaked-but-valid token being used to mutate config looks like normal activity; you see the change, not the abuse.
4. **Reads / DQL queries aren't in the config audit log** — so the cost-inflation action (Leech) leaves little config-audit footprint; you find it on the billing/consumption side, after the fact.
5. **Classic audit is off by default and only keeps 30 days** — many tenants have a 30-day (or zero) window on exactly the changes we care about.
6. **The audit log has no monitor of its own.** Every change above is *recorded*, but nothing *alerts* on it unless you built a workflow to watch `dt.system.events`. **Recorded is not noticed** — which is the entire premise of this project.

> **Implication for the troop:** auditing tells you *what changed* after someone goes looking. It does not tell you *that detection broke*. That gap is what the monkeys exist to measure — and gap #6 deserves a monkey of its own (see **Potto**, below).

Sources: [Audit logs overview](https://docs.dynatrace.com/docs/manage/data-privacy-and-security/configuration/audit-logs-overview) · [Audit logs on Grail](https://docs.dynatrace.com/docs/manage/data-privacy-and-security/configuration/audit-logs-grail) · [Audit logs API](https://docs.dynatrace.com/docs/dynatrace-api/environment-api/audit-logs).

---

## B. Best-practice coverage matrix

Dynatrace's own guidance (Settings management, anomaly-detection tuning, alerting
hygiene, access least-privilege, config-as-code) mapped against what the troop
tests today.

| # | Dynatrace best practice | Tested today? | By |
|---|---|---|---|
| 1 | Anomaly detectors tuned, not disabled | ✅ | Marmoset, Gibbon |
| 2 | Alerting profiles scoped & not over/under-filtered | ✅ | Baboon |
| 3 | Notifications/integrations healthy & reachable | ✅ | Ape |
| 4 | Maintenance windows closed when done | ✅ | Mandrill |
| 5 | Routing profile actually routes | ✅ | Howler |
| 6 | Consistent tagging strategy | ✅ | Capuchin |
| 7 | Management zones / segments scoped correctly | ✅ | Langur |
| 8 | Log ingest rules keep the sources you alert on | ✅ | Colobus |
| 9 | Host monitoring mode appropriate (full-stack where needed) | ✅ (host) | Saki |
| 10 | **Failure detection defines failures correctly** | ❌ | — gap |
| 11 | **SLOs defined on the right metric & target** | ❌ | — gap |
| 12 | **API tokens least-privilege & expiring; usage watched** | ❌ | — gap |
| 13 | **OneAgent / ActiveGent auto-update kept current** | ❌ | — gap |
| 14 | **Frequent-issue / problem-merge not hiding real problems** | ❌ | — gap |
| 15 | **Synthetic monitors actually running from live locations** | ❌ | — gap |
| 16 | ~~RUM injection healthy; overload-prevention not starving data~~ | ⛔ | **out of scope — see note below** |
| 17 | **Config-as-code: out-of-band drift is detected** | ❌ | — gap (meta) |
| 18 | **The audit log / meta-monitor is actually watched** | ❌ | — gap (meta) |

Nine practices covered, nine open. The lineup below closes them.

---

## C. The lineup (proposed monkeys)

All designed to the same rule as the existing troop: **FIRE only if `AUTO` (or
`MANUAL`+`OBJECT` on a canary) and blast ≤ `SCOPE`, inside the canary fence.**
Anything wider is noted and stays out of FIRE.

| Monkey | Gap | Target (schema / API) | Mutation | Rev. | Blast | Verdict |
|---|---|---|---|---|---|---|
| **Mangabey** | #10 | `builtin:failure-detection.*` | Redefine so canary 5xx count as success | AUTO | OBJECT | 🐒 FIRE |
| **Gelada** | #11 | SLO API (classic `/api/v2/slo` or platform SLO) | Widen the canary SLO target / swap its metric | AUTO | OBJECT | 🐒 FIRE |
| **Tarsier** | #12 | Tokens API (`/api/v2/apiTokens`) | Revoke/expire a **dedicated canary ingest token**, then push with it | AUTO (recreate) | OBJECT | 🐒 FIRE |
| **Loris** | #13 | `builtin:deployment.oneagent.updates` / `...activegate.updates` | Freeze auto-update for the canary host group | AUTO | SCOPE | 🐒 FIRE |
| **Tamarin** | #14 | `builtin:anomaly-detection.frequent-issues` (Classic) / Davis merge settings | Turn on frequent-issue suppression so a recurring canary problem is muted | AUTO | OBJECT | 🐒 FIRE |
| **Vervet** | #15 | Synthetic API (`/api/v2/synthetic/monitors`) | Disable the canary monitor / move it to an unused location | AUTO | OBJECT | 🐒 FIRE † |
| ~~Proboscis~~ | ~~#16~~ | ~~RUM injection / overload-prevention~~ | **REJECTED — out of scope (see §C.1)** | — | — | ⛔ |
| **Drill** | #17 | any GitOps-managed canary object (Settings) | Make an out-of-band edit, then check whether Monaco/Terraform drift CI flags it | AUTO | OBJECT | 🐒 FIRE |
| **Potto** | #18 | — (watcher, see §D) | Make a reversible canary change, then assert an audit event + any meta-monitor fire | AUTO | OBJECT | 🐒 FIRE (watcher) |

† **Vervet** and **Tarsier** need two small client additions (synthetic monitors; token CRUD) beyond the current Settings-only protocol. Both are read+scoped-write only.

### C.1 Rejected: Proboscis (#16) — out of scope

The troop breaks things **on the Dynatrace side of the glass**: it degrades the
observer's ability to *notice* a fault, never the observed system. Gap #16 (RUM)
is the one place that line is crossed, so it is deliberately not filled.

Dynatrace's RUM agent is JavaScript **injected into the real HTML served to real
end users**. Mutating injection config changes the bytes shipped to a production
browser — risking CSP violations, render breakage, or load-time regressions that
real users feel. That is chaos-testing the *observed application* through
Dynatrace as a side door, which the whole safety model (canary fence,
auto-revert, reversible-only) is not built to contain. The name alone invites the
out-of-bounds injection-mode knob, so no "tame" version is kept either.

The one legitimate sliver — RUM *data-capture* toggles (capture rate / cost) — is
already covered by the existing "disable a capture source, see if the gap is
noticed" pattern (Baboon/Howler/Colobus). It needs no new monkey. **Proboscis is
not built, now or later.**

### Not promoted to FIRE (stay quarantined / caution)

- **Network-zone / ActiveGate-HA chaos** — routing agents to a zone with no ActiveGate is `SCOPE`→`TENANT` and hard to revert cleanly. Lives with **Gorilla**.
- **IAM policy removal at scale** — reversible per-object but too coupled; one change can brick many automations. Lives with **Locust**.
- Everything already in [`destructiveness-index.md`](destructiveness-index.md) under 🔒.

---

## D. A new class: Watchers (assert, don't just break)

Most monkeys ask *"did detection survive the mutation?"*. Two gaps (#17, #18)
are really about **whether the change itself is observable**, so they need a
monkey that makes a tiny reversible change and then **asserts a signal appears**:

- **Potto** — change a canary object → query `dt.system.events` for the matching
  `AUDIT_EVENT` (`event.provider=="SETTINGS"`) within N minutes, and check any
  "config changed" meta-workflow fired. A miss means your audit/meta-monitoring
  has a blind spot (gap #6/#18).
- **Drill** — change a GitOps-managed canary object out-of-band → check the next
  Monaco/Terraform plan reports drift. A miss means config-as-code isn't the
  source of truth people think it is (gap #17).

Watchers are the bridge from "chaos" to "continuous assurance": run them on a
schedule and they become a standing test that your monitoring-of-monitoring works.

---

## E. Suggested build order

1. **Mangabey, Gelada, Tamarin** — pure Settings-object monkeys; reuse `_canary_updates`, no client changes. Cheapest, close gaps #10/#11/#14.
2. **Loris** — Settings toggle, trivial.
3. **Potto (watcher)** — needs a DQL read (`dt.system.events`); adds the first "assert a signal" oracle. High value for the audit question above.
4. **Tarsier, Vervet** — small client extensions (tokens, synthetic).
5. **Drill** — needs a Monaco/Terraform hook; do once a GitOps pipeline exists to test against.

(**Proboscis / #16 is rejected** — see §C.1. It is the only gap the troop will not close, because filling it means reaching into real user-facing pages.)
