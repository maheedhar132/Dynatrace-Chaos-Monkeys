# Canary dashboard

A ready-made dashboard so an operator can *see* chaos experiments land on the
Dynatrace timeline, instead of only reading the CLI. Every run already emits the
signals; this just surfaces them.

What the troop emits per run (see `src/troop/runner.py` and `src/troop/driver.py`):

- `CUSTOM_INFO` timeline markers: `Chaos experiment started: <monkey>` and
  `Chaos experiment ended:<VERDICT>`, with `chaos.run_id`.
- the injected fault, tagged `chaos`, `chaos.run_id`, `chaos.monkey`, `chaos.scope`:
  an `ERROR_EVENT` (EVENT monkeys), the `chaos.canary.fault` metric (METRIC), or a
  `chaos-canary` log line (LOG).

## Classic Dynatrace — import `chaos-canary.json`

```bash
curl -X POST "https://{env}.live.dynatrace.com/api/config/v1/dashboards" \
  -H "Authorization: Api-Token $DT_TOKEN" \
  -H "Content-Type: application/json" \
  -d @dashboards/chaos-canary.json
```

Then edit the **Open problems** tile and replace `REPLACE_WITH_CANARY_MZ_ID` with
your canary management-zone id (the scope `troop canary setup` printed). The
token needs `WriteConfig`. This is a starting point — adjust the metric selector
and filters to your tenant.

## Latest Dynatrace (Grail / DQL) — build the tiles from these queries

Classic dashboards are deprecated on Gen3; on Latest, make a dashboard with these
DQL tiles:

Run markers (one row per experiment phase):

```
fetch events
| filter event.kind == "CUSTOM_INFO" and isNotNull(chaos.run_id)
| fields timestamp, chaos.run_id, chaos.monkey, chaos.phase, event.name
| sort timestamp desc
```

Injected faults:

```
fetch events
| filter toString(chaos) == "true"
| summarize count(), by:{chaos.monkey, chaos.scope, bin(timestamp, 1m)}
```

The fault metric:

```
timeseries sum(chaos.canary.fault), by:{monkey}
```

Problems opened during runs (correlate by time with the markers):

```
fetch dt.davis.problems
| filter matchesValue(management_zones, "chaos-canary-zone")
| fields timestamp, display_id, event.name, event.status
| sort timestamp desc
```

> ⚠️ A `KILLED` verdict on the timeline is a v0 signal, not proof — Dynatrace may
> have merged the re-injected fault into the baseline problem. Verify by hand;
> don't wire it into automation yet.
