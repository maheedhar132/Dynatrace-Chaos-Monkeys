"""`troop doctor`: prove the setup is safe *before* anyone runs a monkey.

Every check is read-only. They answer the questions that, if wrong, turn a
"safe" experiment into an incident:

  * Is a canary fence actually configured (and not the whole environment)?
  * Can the kill switch and backups be written when we need them?
  * Is the tenant reachable?
  * Is the API token least-privilege — exactly the scopes the tool needs, and
    nothing powerful it doesn't?

A check is an error (must fix), a warning (should look), or info.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .client import DynatraceClient
from .config import Config, Tenant

# What the troop actually uses. Anything the token has beyond this is flagged:
# least privilege means the chaos token should not also be able to, say, manage
# users or export data.
NEEDED_SCOPES = frozenset({
    "settings.read", "settings.write",      # the mutation surface
    "events.ingest",                        # fault injection + timeline markers
    "problems.read",                        # the oracle
})
OPTIONAL_SCOPES = frozenset({
    "auditLogs.read",                       # Potto (Classic audit oracle)
    "apiTokens.read", "apiTokens.write",    # Tarsier (canary token revoke/recreate)
    "ExternalSyntheticIntegration",         # Vervet (synthetic monitor toggle)
    "DataImport",                           # some tenants name event ingest this way
})


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"   # "error" | "warn" | "info"


def _writable(path: str) -> bool:
    """Writable if the nearest existing ancestor dir is writable (dir may not
    exist yet — we only need to be able to create it when the time comes)."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.isdir(d) and os.access(d, os.W_OK)


def run_checks(cfg: Config, tenant: Optional[Tenant],
               client: Optional[DynatraceClient] = None) -> list[Check]:
    checks: list[Check] = []

    # --- config / fence (always, no tenant needed) ---
    if not cfg.tenants:
        checks.append(Check("config.tenants", False,
                            "no tenants configured; copy chaos.example.yaml to chaos.yaml"))
        return checks

    if tenant is None:
        checks.append(Check("config.tenant", False,
                            f"no tenant selected; pass --tenant (one of {sorted(cfg.tenants)})"))
        return checks
    checks.append(Check("config.tenant", True, f"using tenant {tenant.id!r}", "info"))

    scopes = tenant.canary_scopes
    if not scopes:
        checks.append(Check("fence.canary_scopes", False,
                            "no canary_scopes: the fence is empty, every run will be refused"))
    elif "environment" in scopes:
        checks.append(Check("fence.canary_scopes", False,
                            "'environment' is in canary_scopes — that is the whole tenant, "
                            "not a canary; remove it"))
    else:
        checks.append(Check("fence.canary_scopes", True,
                            f"canary fence = {sorted(scopes)}", "info"))

    if not tenant.required_tag:
        checks.append(Check("fence.required_tag", False, "required_tag is empty"))
    else:
        checks.append(Check("fence.required_tag", True,
                            f"objects must carry {tenant.required_tag!r}", "info"))

    # --- local safety machinery ---
    checks.append(Check("safety.kill_switch_writable", _writable(cfg.kill_switch_path),
                        f"kill switch path {cfg.kill_switch_path!r} "
                        + ("is writable" if _writable(cfg.kill_switch_path) else "dir not writable"),
                        "error"))
    checks.append(Check("safety.backup_writable", _writable(os.path.join(cfg.backup_dir, "x")),
                        f"backup dir {cfg.backup_dir!r} "
                        + ("is writable" if _writable(os.path.join(cfg.backup_dir, "x"))
                           else "is not writable"),
                        "error" if not _writable(os.path.join(cfg.backup_dir, "x")) else "info"))

    # --- live tenant checks (only when reachable) ---
    if not tenant.live:
        checks.append(Check("tenant.live", True,
                            "not live (needs both a base_url and a token) -> dry-run only; "
                            "reachability + token checks skipped", "info"))
        return checks
    if client is None:
        return checks

    reachable = False
    try:
        reachable = bool(client.ping())
    except Exception as exc:
        checks.append(Check("tenant.reachable", False, f"ping failed: {exc}"))
    if reachable:
        checks.append(Check("tenant.reachable", True, f"{tenant.base_url} responds", "info"))

        try:
            have = set(client.token_scopes())
        except Exception as exc:
            checks.append(Check("token.scopes", False, f"could not look up token scopes: {exc}"))
            return checks

        missing = NEEDED_SCOPES - have
        if missing:
            checks.append(Check("token.required_scopes", False,
                                f"token is missing required scopes: {sorted(missing)}"))
        else:
            checks.append(Check("token.required_scopes", True,
                                "token has every required scope", "info"))

        extra = have - NEEDED_SCOPES - OPTIONAL_SCOPES
        if extra:
            checks.append(Check("token.least_privilege", False,
                                f"token has {len(extra)} scope(s) the tool never needs: "
                                f"{sorted(extra)} — use a least-privilege token", "warn"))
        else:
            checks.append(Check("token.least_privilege", True,
                                "token is least-privilege (no unused powerful scopes)", "info"))

    return checks


def worst(checks: list[Check]) -> str:
    if any(not c.ok and c.severity == "error" for c in checks):
        return "error"
    if any(not c.ok and c.severity == "warn" for c in checks):
        return "warn"
    return "ok"
