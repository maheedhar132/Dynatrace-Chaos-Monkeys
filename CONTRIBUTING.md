# Contributing

Thanks for looking. This is a safety-critical tool, so the bar is less "does it
work?" and more "can it ever break the wrong thing?".

## Setup

```bash
python -m pip install -e ".[dev,http,web]"
PYTHONPATH=src python -m pytest -q
```

Everything runs offline against the in-memory `DryRunClient`; no Dynatrace tenant
is needed to develop or test.

## The rules that aren't negotiable

Read [SAFETY.md](SAFETY.md) first. A change is rejected, no matter how useful, if
it weakens any of these:

- The five names in `PERMANENTLY_DISABLED` stay un-runnable, with **no** flag,
  config, or arming path. `tests/test_safety.py` guards this — don't skip it.
- The `HttpDynatraceClient` gains **no** method that could rotate a tenant token,
  delete a bucket, anonymize, run billing, or uninstall an agent.
- The canary fence keeps requiring **both** an allowed scope and the canary tag.
- Every mutating path stays reversible, journaled, and backed up before removal.

## Adding a monkey

1. It must be `AUTO` (or `MANUAL` + `OBJECT`) reversibility and blast ≤ `SCOPE`.
2. It only touches objects carrying the canary tag.
3. Its fault must actually exercise the config it mutated (coupling), and it
   declares `fault` / `expected_signal`.
4. If it removes or replaces anything, the backup/journal round-trip must have a
   test (see `tests/test_config_backup.py`).
5. If it reaches into the monitored application rather than Dynatrace config, it
   does **not** belong here (see the Proboscis rejection, lineup §C.1).

## Style

Match the surrounding code: dataclasses, small protocols, ASCII-only CLI output
(the tool targets Windows cp1252 consoles too). Keep comments about *why*, not
*what*. Add or update tests with every behavior change; keep `pytest` green.
