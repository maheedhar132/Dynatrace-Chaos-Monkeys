"""Configuration: one or more Dynatrace tenants, plus global safety limits.

Dynatrace auth is *per environment* (per tenant). A tenant is identified by its
environment id and reached at its own base URL with its own API token:

    https://{environment-id}.live.dynatrace.com   + an Api-Token

So the troop is configured with a set of tenants and authenticates each run
against exactly one of them. A token is best supplied through an environment
variable (named per tenant) rather than written into the file.

Note on the word "token": Dynatrace also has a *tenant token* used by OneAgents
to talk to the cluster — rotating THAT is the Kong action and is permanently
disabled. Everything here uses an ordinary scoped **API token**; the tool has no
code path that touches the OneAgent tenant token.

Backward compatible: a flat `base_url` / `token` at the top level (the old
single-tenant shape) is read as a tenant called "default".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Tenant:
    """One Dynatrace environment and its canary fence."""

    id: str
    base_url: str = ""
    token_env: str = "DT_API_TOKEN"      # env var that holds this tenant's API token
    token: str = ""                      # inline token (discouraged; env var wins)
    canary_scopes: frozenset[str] = field(default_factory=frozenset)
    required_tag: str = "chaos-canary"

    def resolve_token(self) -> str:
        """Env var first, inline token second. Empty means force dry-run."""
        return os.environ.get(self.token_env, "").strip() or self.token.strip()

    @property
    def live(self) -> bool:
        return bool(self.base_url and self.resolve_token())


@dataclass
class Config:
    """The whole configuration: tenants + shared safety settings."""

    tenants: dict[str, Tenant]
    default_tenant: Optional[str] = None
    kill_switch_path: str = ".troop-stop"
    max_ttl_seconds: int = 1800
    backup_dir: str = ".troop-backups"
    journal_path: str = ".troop-journal.jsonl"
    canary_manifest_path: str = ".troop-canary.json"
    results_path: str = ".troop-results.jsonl"

    def tenant(self, tenant_id: Optional[str]) -> Optional[Tenant]:
        """Pick a tenant by id, or the default/only one when id is None."""
        if not self.tenants:
            return None
        if tenant_id:
            if tenant_id not in self.tenants:
                raise KeyError(
                    f"unknown tenant {tenant_id!r}; configured: {sorted(self.tenants)}"
                )
            return self.tenants[tenant_id]
        if self.default_tenant and self.default_tenant in self.tenants:
            return self.tenants[self.default_tenant]
        if len(self.tenants) == 1:
            return next(iter(self.tenants.values()))
        return None  # ambiguous: several tenants, no default, none asked for


def _tenant_from_dict(tid: str, raw: dict[str, Any], defaults: dict[str, Any]) -> Tenant:
    return Tenant(
        id=tid,
        base_url=raw.get("base_url", ""),
        token_env=raw.get("token_env", defaults.get("token_env", "DT_API_TOKEN")),
        token=raw.get("token", ""),
        canary_scopes=frozenset(raw.get("canary_scopes", defaults.get("canary_scopes", []))),
        required_tag=raw.get("required_tag", defaults.get("required_tag", "chaos-canary")),
    )


def parse_config(raw: dict[str, Any]) -> Config:
    """Build a Config from an already-loaded mapping. Pure; easy to test."""
    raw = raw or {}
    tenants: dict[str, Tenant] = {}

    if isinstance(raw.get("tenants"), list):
        for item in raw["tenants"]:
            tid = str(item.get("id") or item.get("base_url") or f"tenant-{len(tenants)}")
            tenants[tid] = _tenant_from_dict(tid, item, raw)
    elif isinstance(raw.get("tenants"), dict):
        for tid, item in raw["tenants"].items():
            tenants[str(tid)] = _tenant_from_dict(str(tid), item or {}, raw)

    # Legacy single-tenant shape: top-level base_url/token -> "default".
    if not tenants and (raw.get("base_url") or raw.get("token") or raw.get("canary_scopes")):
        tenants["default"] = _tenant_from_dict("default", raw, raw)

    return Config(
        tenants=tenants,
        default_tenant=raw.get("default_tenant"),
        kill_switch_path=raw.get("kill_switch_path", ".troop-stop"),
        max_ttl_seconds=int(raw.get("max_ttl_seconds", 1800)),
        backup_dir=raw.get("backup_dir", ".troop-backups"),
        journal_path=raw.get("journal_path", ".troop-journal.jsonl"),
        canary_manifest_path=raw.get("canary_manifest_path", ".troop-canary.json"),
        results_path=raw.get("results_path", ".troop-results.jsonl"),
    )


def load_config(path: Optional[str]) -> Config:
    """Read a YAML config file. A missing path yields an empty (dry-run) config."""
    if not path or not os.path.exists(path):
        return parse_config({})
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return parse_config(yaml.safe_load(fh) or {})
