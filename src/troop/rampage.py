"""Randomized chaos: let the troop pick which monkey jumps.

Two ways to run the troop:
  * pick a specific monkey (the deterministic path — `run <monkey>`), or
  * let it choose one at random (`rampage`).

Random mode is never *silent* random: every choice is logged — the candidate
pool, the seed, and why the winner was picked — so a run is always reproducible
and explainable after the fact.
"""

from __future__ import annotations

import random
from typing import Optional

from .client import DynatraceClient
from .models import Fault
from .monkeys import FIRE
from .safety import PERMANENTLY_DISABLED


def eligible_monkeys(live_only: bool = True) -> list[str]:
    """FIRE monkeys only, never anything matching the off-switch.

    By default only *live* (driver-injectable) monkeys are eligible, so a random
    rampage doesn't keep landing on traffic/external-gated monkeys that would
    just return INCONCLUSIVE. Pass `live_only=False` to include the gated ones.
    """
    return sorted(
        n for n, cls in FIRE.items()
        if not any(d == n.lower() or d in n.lower() for d in PERMANENTLY_DISABLED)
        and (not live_only or getattr(cls, "fault", Fault.EVENT).injectable)
    )


def choose(client: DynatraceClient, scope: str, *, seed: Optional[int] = None,
           require_changes: bool = True, live_only: bool = True
           ) -> tuple[Optional[str], list[str]]:
    """Choose one monkey at random. Returns (name, log_lines).

    When `require_changes` is set, only monkeys whose plan actually finds a
    canary-tagged target to touch are considered (so a rampage does something).
    If none qualify — e.g. an empty dry-run store — it falls back to the full
    eligible pool and says so, yielding a monkey that will simply no-op.

    `live_only` (default True) restricts the pool to event-drivable monkeys; a
    rampage over gated monkeys would mostly produce INCONCLUSIVE.
    """
    rng = random.Random(seed)
    pool = eligible_monkeys(live_only=live_only)
    log: list[str] = [f"[rampage] seed={seed!r}",
                      f"[rampage] pool={'live-only' if live_only else 'live+gated'}",
                      f"[rampage] eligible: {pool}"]

    actionable: list[str] = []
    if require_changes:
        for name in pool:
            try:
                if FIRE[name]().plan(client, scope).changes:
                    actionable.append(name)
            except Exception as exc:
                log.append(f"[rampage] {name} skipped (plan failed: {exc})")
        log.append(f"[rampage] with canary targets in {scope!r}: {actionable or 'none'}")

    candidates = actionable or pool
    if not candidates:
        log.append("[rampage] no eligible monkeys at all")
        return None, log
    if require_changes and not actionable:
        log.append("[rampage] no monkey found a canary target; picking from full pool "
                   "(the run will no-op)")

    chosen = rng.choice(candidates)
    log.append(f"[rampage] chose: {chosen}")
    return chosen, log
