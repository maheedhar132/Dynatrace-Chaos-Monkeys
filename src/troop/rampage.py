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
from .monkeys import FIRE
from .safety import PERMANENTLY_DISABLED


def eligible_monkeys() -> list[str]:
    """FIRE monkeys only, and never anything matching the off-switch."""
    return sorted(
        n for n in FIRE
        if not any(d == n.lower() or d in n.lower() for d in PERMANENTLY_DISABLED)
    )


def choose(client: DynatraceClient, scope: str, *, seed: Optional[int] = None,
           require_changes: bool = True) -> tuple[Optional[str], list[str]]:
    """Choose one monkey at random. Returns (name, log_lines).

    When `require_changes` is set, only monkeys whose plan actually finds a
    canary-tagged target to touch are considered (so a rampage does something).
    If none qualify — e.g. an empty dry-run store — it falls back to the full
    eligible pool and says so, yielding a monkey that will simply no-op.
    """
    rng = random.Random(seed)
    pool = eligible_monkeys()
    log: list[str] = [f"[rampage] seed={seed!r}", f"[rampage] eligible: {pool}"]

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
