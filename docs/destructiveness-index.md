# Destructiveness index

Every chaos action a tool *could* take against a Dynatrace tenant, ranked by **how hard it is to undo** and **how wide the damage spreads**. This is the risk map the troop is built on: it decides which monkeys may fire and which are quarantined.

## Scoring

**Reversibility** — can the tool put it back?

| Code | Meaning |
|---|---|
| `AUTO` | Tool reverts it from a snapshot. Clean. |
| `MANUAL` | A human can fix it, but the tool can't (needs a restart, re-push, re-auth). |
| `NONE` | No clean undo. Data or trust is gone. |

**Blast radius** — who feels it.

| Code | Meaning |
|---|---|
| `OBJECT` | One settings object / one entity. |
| `SCOPE` | One management zone / host group / segment. |
| `TENANT` | The whole environment. |
| `FLEET` | Every connected OneAgent, or billing. |

**Verdict**

- 🐒 **FIRE** — reversible + scoped. A real experiment. Implemented.
- 🔒 **QUARANTINE** — irreversible, fleet-wide, or financial. Modeled and classified, execution hard-blocked.

---

## The index

| Action | Reversibility | Blast | Why | Verdict | Monkey |
|---|---|---|---|---|---|
| Disable / inflate a **canary** anomaly detector | `AUTO` | `OBJECT` | Snapshot the object, PUT it back | 🐒 FIRE | Marmoset |
| Raise min severity / add exclusion on a **canary** alerting profile | `AUTO` | `OBJECT` | Snapshot + restore | 🐒 FIRE | Baboon |
| Disable / delete a **canary** problem notification | `AUTO` | `OBJECT` | Snapshot the object before delete; recreate from it | 🐒 FIRE | Ape |
| Insert a short, TTL'd maintenance window over the **canary** scope | `AUTO` | `SCOPE` | Delete the window we created | 🐒 FIRE | Mandrill |
| Create a clearly-tagged **canary** alerting profile + sink integration in a canary MZ | `AUTO` | `SCOPE` | Delete what we created; TTL-bounded | 🐒 FIRE | Howler |
| Break a **canary** auto-tag / MZ rule | `AUTO` | `OBJECT` | Snapshot + restore | 🐒 FIRE | Baboon (variant) |
| Revoke a dedicated **canary** ingest token | `AUTO` | `OBJECT` | Recreate the token; only the canary used it | 🐒 FIRE | Ape (variant) |
| Downgrade monitoring mode on a **canary host** | `MANUAL` | `OBJECT` | PUT the setting back, but the host may need a restart to re-deep-monitor | 🐒 FIRE (canary host only) | Marmoset (variant) |
| **Mass** monitoring-mode downgrade / disable across a scope | `MANUAL` | `SCOPE`→`TENANT` | Re-deep-monitoring needs process restarts fleet-wide | 🔒 QUARANTINE | Gorilla |
| **Tenant token rotation** | `NONE`* | `FLEET` | Disconnects every OneAgent; reconnect needs the new token pushed to each agent. No clean revert | 🔒 QUARANTINE | Kong |
| **Bulk DQL / query cost inflation** | `NONE` | `FLEET` (billing) | Consumed DDUs/DPS can't be refunded. Pure financial harm, no hypothesis | 🔒 QUARANTINE | Leech |
| **Unfiltered tenant-wide** alerting profile + integration | `AUTO`-ish | `TENANT` | Floods on-call. Noise weapon, not a scoped test | 🔒 QUARANTINE | Locust |
| **Hard delete** of config at scale / Grail bucket deletion / anonymization jobs | `NONE` | `TENANT`→`FLEET` | Destroys data and history irreversibly | 🔒 QUARANTINE | Reaper |
| OneAgent uninstall / deployment changes | `MANUAL`/`NONE` | `FLEET` | Fleet-wide, outside config | 🔒 QUARANTINE | Reaper (variant) |
| Remove an IAM policy from a workflow's service user | `AUTO` if we re-add | `TENANT` | Reversible but can brick many automations at once; too coupled | 🔒 QUARANTINE | Locust (variant) |

\* "Finishing" a rotation doesn't reconnect agents; that's why it scores `NONE` in practice.

## The rule the troop enforces

> A monkey may fire only if **reversibility is `AUTO`** (or `MANUAL` *and* blast is `OBJECT` on a canary) **and** blast is `OBJECT` or `SCOPE` **and** every target is inside the canary fence.
> Anything `NONE`, anything `FLEET`, and anything `TENANT`-wide is quarantined — classified here, never executed.

Source schemas and endpoints behind each row: [`api-index.md`](api-index.md) and [`settings-schemas.md`](settings-schemas.md).
