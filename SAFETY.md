# Safety model & threat model

This is a tool that **deliberately breaks monitoring configuration**. It is only
safe because of the guarantees below. If you change the code, you are changing a
safety-critical tool — read this first, and keep every invariant.

> **Disclaimer.** The software is provided "as is", with no warranty (see
> [LICENSE](LICENSE)). Running it against a production Dynatrace environment can
> suppress real alerts. You are responsible for scoping it to a canary and for
> anything it touches. The maintainers accept no liability for missed incidents,
> data loss, or cost.
>
> **Not affiliated with Dynatrace.** "Dynatrace" is a trademark of Dynatrace LLC.
> This is an independent, unofficial project, not affiliated with, authorized by,
> or endorsed by Dynatrace.

## What it does

Mutation testing for observability config: mutate one setting → inject a fault
that *should* alarm → check whether Dynatrace still detects it → revert. A broken
detection means the config was load-bearing (**KILLED**); an alarm that still
fires means it was dead weight (**SURVIVED**); anything it can't decide is
**INCONCLUSIVE** — never silently treated as SURVIVED.

It only ever degrades Dynatrace's ability to *notice* things. It never reaches
into the systems Dynatrace monitors (this is why RUM injection was rejected — see
[docs/monkey-lineup.md](docs/monkey-lineup.md) §C.1).

## The guarantees (invariants that must never regress)

1. **Permanent off-switch.** The five tenant-wrecking actions
   (`kong`, `gorilla`, `leech`, `locust`, `reaper`) can never execute. There is
   no flag, config value, or arming step that enables them. The guard, the
   runner, the CLI, and the HTTP client all refuse them, and the HTTP client has
   no method capable of carrying them out (no tenant-token rotation, bucket
   deletion, anonymization, billing, or agent uninstall).
2. **Canary fence (two keys).** A change is in-bounds only if its scope is a
   configured `canary_scope` **and** the object carries the `chaos-canary` tag.
   Either alone is refused. `environment` is never a valid canary scope.
3. **Blast & reversibility ceilings.** The guard refuses any plan wider than
   `SCOPE` or less reversible than `MANUAL`.
4. **Baseline gate.** Nothing is mutated until the fault is shown to be
   detectable first. No false "KILLED".
5. **Write-ahead journal.** Every change is flushed to disk *before* it is
   applied, so a crash mid-run is still fully revertible (`troop restore`).
6. **Full pre-removal backup.** Before any delete/overwrite, the complete live
   object is snapshotted to `backup_dir/<run_id>/`, independent of the journal.
7. **Revert + verification.** Every run reverts in a `finally` block, then
   re-reads the objects and diffs them against their pre-state.
8. **Kill switch + TTL.** Creating the `.troop-stop` file halts and forces a
   revert; nothing stays mutated past `max_ttl_seconds`.
9. **Least privilege.** `troop doctor` refuses to bless a token that is missing
   required scopes or carries powerful scopes the tool never needs.

## Do not automate on verdicts (v0)

This is a v0 oracle, and its verdicts are **advisory input for a human**, not
triggers for machines. In particular, **do not wire `KILLED` into automated
remediation, paging, or config deletion.** A `KILLED` can be a false positive:
Dynatrace may merge the re-injected fault into the problem the baseline opened,
so no *new* problem appears and detection reads as broken when it was fine. The
tool surfaces this everywhere a verdict is reported — a `messages` line on every
`KILLED`, a `[CAUTION]` line in the CLI, and a `caution` field in the HTTP
response — precisely so nobody automates on it prematurely. Likewise, trust a
`SURVIVED` (delete-candidate) verdict only when you know a real fault exercised
the config; a `GATED` monkey run without the workload it needs returns
`INCONCLUSIVE`, which the tool labels, rather than a false `SURVIVED`.

## Trust boundary / threat model

- **The oracle is the thing to distrust most.** Dynatrace detection has latency;
  set `--oracle-wait` to your Davis window. A `0` wait on a real run is flagged
  as unreliable. Treat a verdict produced with no wait as advisory only. The
  post-mutation check credits only a *new* problem (this run's id, an exact
  canary-zone match, or the chaos tag) and excludes anything already open at
  baseline, so a lingering problem can't read as a false `SURVIVED`.
- **The fault driver injects only synthetic signals.** The driver pushes an
  `ERROR_EVENT`, a canary metric (`chaos.canary.fault`), or a `chaos-canary` log
  line — all tagged `chaos`/`chaos.run_id` so they are never mistaken for real
  telemetry — over the ingest APIs. It never reaches into a monitored system; the
  reference canary service (`examples/canary_service.py`) is a throwaway workload
  you run yourself, not something the tool drives inside your estate.
- **Live vs gated monkeys.** Only EVENT/METRIC/LOG faults are injectable, so those
  monkeys are live now; `canary setup` also provisions the matching detectors (a
  metric event, a log event) so the injected fault actually opens a problem.
  TRACE-gated monkeys need real service traffic — `troop run --drive-canary <url>`
  generates HTTP load against the reference canary service for the run (it touches
  nothing in Dynatrace); out-of-band monkeys need the real fault. Otherwise they
  return `INCONCLUSIVE`. `troop list`, `plan`, `run`, and `rampage` all surface
  this so a gated monkey never produces a misleading verdict. `troop verify`
  proves each monkey's schema against your tenant (validate-only; never writes).
- **Verdicts are advisory, and the report says so.** `troop report` prints the
  mutation score and the don't-automate caution on every render; a KILLED or
  SURVIVED verdict is a prompt for human review, never an automation trigger.
- **`troop selftest` mutates no config.** It injects faults and reads problems to
  confirm the provisioned detectors actually fire; it changes no settings object.
  Combined with `--setup`/`--teardown` it can provision and remove the canary
  around the probe, using the same journalled, backed-up machinery as any run.
- **Config is data, not instructions.** Objects read from the tenant are never
  executed or trusted as commands.
- **Secrets.** Tokens come from per-tenant environment variables, never the repo.
  `chaos.yaml` is git-ignored; only `chaos.example.yaml` is committed.
- **Nothing runs without `--execute`** and a configured fence a plan clears.

## Reporting a safety issue

Open an issue describing the invariant at risk and a reproduction. Do not include
real tokens, tenant ids, or customer data.
