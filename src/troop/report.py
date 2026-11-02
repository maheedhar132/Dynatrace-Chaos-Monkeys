"""The scorecard: turn a pile of runs into a number and a table a team acts on.

A single run prints log lines; a *campaign* of runs needs a verdict a reviewer
can read in ten seconds — the **mutation score**, and the list of config that
looks dead. `ResultStore` persists one row per run (append-only JSONL); `report`
aggregates them into JSON and Markdown.

Mutation score = KILLED / (KILLED + SURVIVED). High is good: most of your config
is load-bearing. A low score means a lot of config isn't catching anything.
INCONCLUSIVE runs are excluded from the score (you couldn't decide) but counted
and listed, because an inconclusive baseline is often the most important finding.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import ExperimentResult, Plan


def result_row(result: ExperimentResult, plan: Optional[Plan] = None,
               scope: str = "", tenant: str = "") -> dict[str, Any]:
    """Flatten one run into a persistable row."""
    targets = [str(c.target) for c in plan.changes] if plan else []
    return {
        "ts_ms": int(time.time() * 1000),
        "monkey": result.monkey,
        "run_id": result.run_id,
        "tenant": tenant,
        "scope": scope,
        "executed": result.executed,
        "outcome": None if result.outcome is None else result.outcome.name,
        "revert_verified": result.revert_verified,
        "targets": targets,
    }


@dataclass
class ResultStore:
    """Append-only JSONL of run rows."""

    path: str
    _rows: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            self._rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

    def append(self, row: dict[str, Any]) -> None:
        self._rows.append(row)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def clear(self) -> None:
        self._rows = []
        if os.path.exists(self.path):
            os.remove(self.path)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate rows into a scorecard dict."""
    # A run counts if it reached a verdict. INCONCLUSIVE runs have executed=False
    # (the baseline never fired, so nothing was mutated) but still carry a verdict.
    scored = [r for r in rows if r.get("outcome")]
    killed = [r for r in scored if r.get("outcome") == "KILLED"]
    survived = [r for r in scored if r.get("outcome") == "SURVIVED"]
    inconclusive = [r for r in scored if r.get("outcome") == "INCONCLUSIVE"]
    decided = len(killed) + len(survived)
    score = (len(killed) / decided) if decided else None
    return {
        "generated_ms": int(time.time() * 1000),
        "runs_total": len(rows),
        "runs_scored": len(scored),
        "killed": len(killed),
        "survived": len(survived),
        "inconclusive": len(inconclusive),
        "mutation_score": score,                 # None when nothing decided
        # the deliverable: config that still fired after being broken = dead/redundant
        "dead_config_candidates": _dedup([t for r in survived for t in r.get("targets", [])]),
        "load_bearing": _dedup([t for r in killed for t in r.get("targets", [])]),
        "inconclusive_monkeys": _dedup([r.get("monkey", "?") for r in inconclusive]),
    }


def _dedup(items: list[str]) -> list[str]:
    seen: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.append(i)
    return seen


def to_markdown(summary: dict[str, Any]) -> str:
    score = summary["mutation_score"]
    score_s = "n/a (nothing decided)" if score is None else f"{score:.0%}"
    lines = [
        "# Chaos Troop - mutation report",
        "",
        f"- runs: **{summary['runs_total']}** ({summary['runs_scored']} reached a verdict)",
        f"- KILLED (load-bearing): **{summary['killed']}**",
        f"- SURVIVED (dead/redundant): **{summary['survived']}**",
        f"- INCONCLUSIVE: **{summary['inconclusive']}**",
        f"- **mutation score: {score_s}**  (killed / decided)",
        "",
        "> WARNING: v0 oracle - a verdict is input for a human, not a trigger for automation. "
        "Do not wire KILLED or SURVIVED into automated remediation or config deletion yet.",
        "",
        "## Dead / redundant config candidates (SURVIVED - review for deletion)",
    ]
    cand = summary["dead_config_candidates"]
    lines += ([f"- `{t}`" for t in cand] or ["- (none)"])
    lines += ["", "## Load-bearing config (KILLED - protect it)"]
    lines += ([f"- `{t}`" for t in summary["load_bearing"]] or ["- (none)"])
    if summary["inconclusive_monkeys"]:
        lines += ["", "## Inconclusive (gated monkeys, or the monitoring is already blind)",
                  "- " + ", ".join(summary["inconclusive_monkeys"])]
    lines.append("")
    return "\n".join(lines)


def to_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, indent=2)


# --- trend (score over time) -----------------------------------------------

def parse_duration(s: str) -> int:
    """'7d' / '24h' / '90m' / '30s' -> seconds. A bare number is seconds."""
    s = s.strip().lower()
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    if s and s[-1] in units:
        return int(float(s[:-1]) * units[s[-1]])
    return int(float(s))


def filter_since(rows: list[dict[str, Any]], since_ms: int) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("ts_ms", 0) >= since_ms]


def trend(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mutation score per UTC day, oldest first — the number only means something
    as a trend. A snapshot tells you today; this tells you whether your config is
    getting more or less load-bearing over time."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if not r.get("outcome"):
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(r.get("ts_ms", 0) / 1000))
        buckets.setdefault(day, []).append(r)
    out: list[dict[str, Any]] = []
    for day in sorted(buckets):
        s = summarize(buckets[day])
        out.append({"day": day, "killed": s["killed"], "survived": s["survived"],
                    "inconclusive": s["inconclusive"], "mutation_score": s["mutation_score"]})
    return out


def trend_markdown(rows: list[dict[str, Any]]) -> str:
    t = trend(rows)
    if not t:
        return "## Trend\n\n(no scored runs yet)\n"
    lines = ["## Trend (mutation score by day)", "",
             "| day | killed | survived | inconclusive | score |",
             "|---|---|---|---|---|"]
    for b in t:
        score = "n/a" if b["mutation_score"] is None else f"{b['mutation_score']:.0%}"
        lines.append(f"| {b['day']} | {b['killed']} | {b['survived']} | "
                     f"{b['inconclusive']} | {score} |")
    lines.append("")
    return "\n".join(lines)
