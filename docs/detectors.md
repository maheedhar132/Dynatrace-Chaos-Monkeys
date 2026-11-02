# Detectors: making the live faults actually fire

The EVENT monkeys fire natively — an ingested `ERROR_EVENT` opens a problem on
its own. The **METRIC** and **LOG** monkeys don't: a metric point or a log line
only becomes a problem if a *detector* is configured to watch for it. `troop
canary setup` provisions those detectors (a metric event and a log event), but a
detector shape that is schema-valid can still fail to *trip* — that is the one
thing only a live tenant tells you. This page is the known-good config and how to
tune it, and `troop selftest` is how you confirm it.

## Prove it first

```bash
troop selftest --tenant prod-canary        # injects each live fault, checks a problem opened
```

`[ OK ]` per monkey means the whole loop works on your tenant. A `[FAIL]` on
`marmoset` or `colobus` means the detector or the push cadence needs tuning —
read on. (Self-test injects faults and reads problems; it mutates no config.)

## Metric event (marmoset) — `builtin:anomaly-detection.metric-events`

The driver pushes `chaos.canary.fault` (one point per injection; `selftest`
sends a short burst). The provisioned event alerts when that metric goes above a
threshold. The shape the kit provisions:

```json
{
  "enabled": true,
  "queryDefinition": { "type": "METRIC_KEY", "metricKey": "chaos.canary.fault", "aggregation": "SUM" },
  "modelProperties": {
    "type": "STATIC_THRESHOLD", "threshold": 0, "alertCondition": "ABOVE",
    "alertingOnMissingData": false,
    "violatingSamples": 1, "samples": 1, "dealertingSamples": 1
  }
}
```

Why these values, and what to tune if it doesn't fire:

- **`threshold: 0` + `ABOVE`** — any pushed value (the driver sends `1.0`) is a
  violation. If your tenant rejects a 0 threshold, use `0.5`.
- **`violatingSamples: 1`, `samples: 1`** — one data point trips it, so you don't
  have to sustain the metric. If it won't fire on a single point, raise the
  driver's burst (`troop drive --kind metric --count 5`) or widen `samples`.
- **Evaluation cadence** — metric events evaluate roughly once a minute, so set
  `--oracle-wait` to at least a couple of minutes on a real run; a 0 wait will
  look like a miss even when the config is correct.
- **The metric must exist first** — the very first push *creates* the metric; the
  event may only start evaluating on the next cadence. `selftest` bursts to cover
  this; on a cold tenant, run it twice.

## Log event (colobus) — `builtin:logmonitoring.log-events`

The driver pushes a log line containing `chaos-canary`; the event alerts on it:

```json
{
  "enabled": true,
  "query": "content=\"chaos-canary\"",
  "queryType": "CUSTOM",
  "alertAfterNumberOfOccurrences": 1
}
```

- **`alertAfterNumberOfOccurrences: 1`** — one matching line trips it.
- **colobus's interaction** is genuinely tenant-dependent: it adds a
  *storage-exclusion* rule to `builtin:logmonitoring.log-storage-settings`. Whether
  excluding the log from storage also stops the log event from evaluating is a
  real behaviour to confirm on your tenant — that is exactly the thing the monkey
  is testing. Either verdict is meaningful; `selftest` confirms the baseline
  (pre-exclusion) fires.
- **Log ingest licensing** — the log must be accepted by your log pipeline. If
  `selftest` shows `colobus [FAIL]` but `marmoset [ OK ]`, check that generic log
  ingest is enabled and the token has the log ingest scope.

## TRACE monkeys

These can't be driven by an ingest API at all — they need a real service to
degrade. Run the reference canary under OneAgent and let the run drive it:

```bash
python examples/canary_service.py --port 8099
troop run gibbon --scope <id> --execute --oracle-wait 600 --drive-canary http://localhost:8099
```

See [`../dashboards/README.md`](../dashboards/README.md) to watch any of this land
on the Dynatrace timeline.

> The shapes here are representative and validated for presence with `troop
> verify`; the firing behaviour is what `troop selftest` confirms. Tune, re-run
> selftest, and you have a loop you can trust on your tenant.
