"""`troop` command line.

    troop list                       show the whole troop and verdicts
    troop tenants                    show configured tenants
    troop plan    <monkey> [--scope] dry-run: what it would do (no tenant needed)
    troop run     <monkey> --execute real run (needs config); dry-run without --execute
    troop rampage [--seed]           let the troop pick a monkey at random (logged)
    troop watch   <watcher>          probe + assert a signal appeared
    troop restore                    replay the journal and put everything back

Config comes from a YAML file (see chaos.example.yaml): one or more tenants,
each with its base_url + API token (env var) + canary fence, plus global safety
limits. Pick a tenant with --tenant; without --execute, nothing touches it.

Run it either from here, or from the Swagger UI: `troop-web` (see troop.web).
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .backup import BackupVault
from .client import DryRunClient, DynatraceClient, HttpDynatraceClient
from .config import Config, Tenant, load_config
from .journal import Journal
from .monkeys import FIRE, QUARANTINE, WATCHERS
from .preflight import run_checks, worst
from .rampage import choose as rampage_choose
from .runner import Runner
from .safety import Fence, Guard, SafetyError, assert_executable


# --- wiring: config -> client / guard / vault -------------------------------

def _resolve_tenant(cfg: Config, args) -> Optional[Tenant]:
    try:
        t = cfg.tenant(getattr(args, "tenant", None))
    except KeyError as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        raise SystemExit(2)
    if t is None and cfg.tenants and getattr(args, "tenant", None) is None:
        print(f"[STOP] several tenants configured and none selected; "
              f"pass --tenant <id> (one of: {sorted(cfg.tenants)})", file=sys.stderr)
        raise SystemExit(2)
    return t


def _client_for(tenant: Optional[Tenant], dry_run: bool) -> DynatraceClient:
    if dry_run or tenant is None or not tenant.live:
        if not dry_run and tenant is not None and tenant.base_url and not tenant.resolve_token():
            print(f"no token for tenant {tenant.id!r} "
                  f"(set ${tenant.token_env}); staying in dry-run", file=sys.stderr)
        return DryRunClient()
    return HttpDynatraceClient(tenant.base_url, tenant.resolve_token())


def _readonly_client_for(tenant: Optional[Tenant]) -> DynatraceClient:
    """For doctor: read against the real tenant if it's live, else dry-run."""
    if tenant is not None and tenant.live:
        return HttpDynatraceClient(tenant.base_url, tenant.resolve_token())
    return DryRunClient()


def _guard_for(cfg: Config, tenant: Optional[Tenant], dry_run: bool) -> Guard:
    fence = Fence(
        allowed_scopes=tenant.canary_scopes if tenant else frozenset(),
        required_tag=tenant.required_tag if tenant else "chaos-canary",
        kill_switch_path=cfg.kill_switch_path,
        max_ttl_seconds=cfg.max_ttl_seconds,
    )
    return Guard(fence=fence, dry_run=dry_run)


def _vault_for(cfg: Config, args) -> Optional[BackupVault]:
    if getattr(args, "no_backup", False):
        return None
    return BackupVault(getattr(args, "backup_dir", None) or cfg.backup_dir)


def _journal_for(cfg: Config, args) -> Journal:
    return Journal(getattr(args, "journal", None) or cfg.journal_path)


# --- commands ---------------------------------------------------------------

def cmd_list(_args) -> int:
    print("FIRE monkeys (reversible, canary-scoped - these run):\n")
    for name, cls in FIRE.items():
        inst = cls()
        print(f"  [FIRE] {name:10s} {inst.reversibility!s:6s} {inst.blast!s:7s}")
    print("\nWATCHERS (probe + assert a signal appeared — run with `watch`):\n")
    for name in WATCHERS:
        print(f"  [WTCH] {name:10s} (probe a canary change, check it was recorded)")
    print("\nQUARANTINE monkeys (classified, never executed):\n")
    for name, q in QUARANTINE.items():
        print(f"  [LOCK] {name:10s} {q.reversibility!s:6s} {q.blast!s:7s}  {q.what}")
    print("\nSee docs/destructiveness-index.md for the full ranking.")
    return 0


def cmd_tenants(args) -> int:
    cfg = load_config(args.config)
    if not cfg.tenants:
        print("no tenants configured (dry-run only). See chaos.example.yaml.")
        return 0
    print("configured tenants:\n")
    for tid, t in cfg.tenants.items():
        state = "LIVE" if t.live else ("needs token" if t.base_url else "dry-run")
        default = "  (default)" if tid == cfg.default_tenant else ""
        print(f"  {tid:16s} {state:12s} {t.base_url or '-'}{default}")
        print(f"  {'':16s} token env ${t.token_env}; "
              f"canary {sorted(t.canary_scopes) or '[]'}; tag {t.required_tag!r}")
    return 0


def cmd_plan(args) -> int:
    if args.monkey in QUARANTINE:
        q = QUARANTINE[args.monkey]
        print(f"[LOCK] {args.monkey} is QUARANTINED — no plan.\n   {q.what}\n   blocked: {q.why_blocked}")
        return 2
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    client = _client_for(tenant, dry_run=True)
    plan = FIRE[args.monkey]().plan(client, args.scope)
    print(f"monkey:   {plan.monkey}\nwhat:     {plan.description}")
    print(f"risk:     {plan.reversibility}/{plan.blast}")
    print(f"fault:    {plan.fault}\nexpected: {plan.expected_signal}")
    print(f"changes:  {len(plan.changes)}")
    for ch in plan.changes:
        print(f"  - {ch.target}: {ch.note}")
    if not plan.changes:
        print("  (none - no canary-tagged objects found in this scope)")
    return 0


def _run_one(name: str, cfg: Config, tenant: Optional[Tenant], args, dry: bool) -> int:
    try:
        assert_executable(name)   # permanent off-switch, before anything loads
    except SafetyError as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        return 2
    if name in QUARANTINE:
        print(f"[LOCK] refusing: {name} is quarantined.", file=sys.stderr)
        return 2
    if not dry and args.oracle_wait == 0:
        print("[warn] --oracle-wait is 0: detection latency is not accounted for, so a "
              "verdict may be unreliable. For real runs set it to your Davis window "
              "(e.g. --oracle-wait 600).", file=sys.stderr)
    client = _client_for(tenant, dry_run=dry)
    guard = _guard_for(cfg, tenant, dry_run=dry)
    runner = Runner(client, guard, _journal_for(cfg, args), vault=_vault_for(cfg, args))
    try:
        plan = FIRE[name]().plan(client, args.scope)
        result = runner.run(plan, ttl_seconds=args.ttl,
                            oracle_wait_s=args.oracle_wait, poll_s=args.poll)
    except SafetyError as exc:
        print(f"[STOP] guard refused: {exc}", file=sys.stderr)
        return 2
    for m in result.messages:
        print(m)
    if result.outcome is not None:
        print(f"\nverdict: {result.outcome}")
    elif result.executed and result.killed is not None:
        print(f"\nverdict: {'KILLED' if result.killed else 'SURVIVED'}")
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    return _run_one(args.monkey, cfg, tenant, args, dry=not args.execute)


def cmd_rampage(args) -> int:
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    dry = not args.execute
    client = _client_for(tenant, dry_run=dry)
    rc = 0
    for i in range(max(1, args.count)):
        seed = None if args.seed is None else args.seed + i
        name, log = rampage_choose(client, args.scope, seed=seed)
        for line in log:
            print(line)
        if name is None:
            print("[rampage] nothing to run.")
            return 0
        print(f"\n=== rampage {i + 1}/{args.count}: {name} ===")
        rc = _run_one(name, cfg, tenant, args, dry=dry) or rc
    return rc


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    client = _readonly_client_for(tenant)
    checks = run_checks(cfg, tenant, client)
    mark = {"error": "[FAIL]", "warn": "[WARN]", "info": "[ -- ]"}
    for c in checks:
        tag = "[ OK ]" if c.ok else mark.get(c.severity, "[FAIL]")
        print(f"  {tag} {c.name:28s} {c.detail}")
    w = worst(checks)
    print({"ok": "\nall clear - safe to run.",
           "warn": "\nusable, but review the warnings above.",
           "error": "\nNOT safe to run until the errors above are fixed."}[w])
    return 0 if w != "error" else 2


def cmd_watch(args) -> int:
    if args.watcher not in WATCHERS:
        print(f"unknown watcher {args.watcher!r}; try: {', '.join(WATCHERS)}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    dry = not args.execute
    client = _client_for(tenant, dry_run=dry)
    guard = _guard_for(cfg, tenant, dry_run=dry)
    watcher = WATCHERS[args.watcher]()
    try:
        result = watcher.run(client, guard, _journal_for(cfg, args), args.scope,
                             timeout_s=args.timeout)
    except SafetyError as exc:
        print(f"[STOP] guard refused: {exc}", file=sys.stderr)
        return 2
    for m in result.messages:
        print(m)
    if result.executed and result.killed is not None:
        print(f"\nverdict: {'BLIND SPOT' if result.killed else 'OBSERVABLE'}")
    return 0


def cmd_restore(args) -> int:
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    client = _client_for(tenant, dry_run=not args.execute)
    journal = _journal_for(cfg, args)
    if journal.pending() == 0:
        print("nothing to restore.")
        return 0
    for u in journal.restore(client):
        print(f"reverted: {u}")
    print(f"done; {journal.pending()} pending.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="troop", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="chaos.yaml", help="YAML config (default: chaos.yaml)")
    p.add_argument("--journal", default=None, help="write-ahead journal path (overrides config)")
    p.add_argument("--tenant", default=None, help="which configured tenant to use")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the troop").set_defaults(func=cmd_list)
    sub.add_parser("tenants", help="show configured tenants").set_defaults(func=cmd_tenants)
    sub.add_parser("doctor", help="preflight: is the setup safe to run?").set_defaults(func=cmd_doctor)

    pp = sub.add_parser("plan", help="dry-run a monkey")
    pp.add_argument("monkey")
    pp.add_argument("--scope", default="environment")
    pp.set_defaults(func=cmd_plan)

    def _exec_args(sp):
        sp.add_argument("--scope", default="environment")
        sp.add_argument("--execute", action="store_true", help="actually touch the tenant")
        sp.add_argument("--ttl", type=int, default=300, help="auto-revert deadline (s)")
        sp.add_argument("--oracle-wait", dest="oracle_wait", type=float, default=0.0,
                        help="seconds to wait for detection (set to your Davis window)")
        sp.add_argument("--poll", type=float, default=2.0, help="oracle poll interval (s)")
        sp.add_argument("--backup-dir", dest="backup_dir", default=None,
                        help="where to write pre-removal backups (overrides config)")
        sp.add_argument("--no-backup", dest="no_backup", action="store_true",
                        help="skip pre-removal backups (not recommended)")

    pr = sub.add_parser("run", help="run a monkey (dry-run unless --execute)")
    pr.add_argument("monkey")
    _exec_args(pr)
    pr.set_defaults(func=cmd_run)

    pg = sub.add_parser("rampage", help="let the troop pick a monkey at random (logged)")
    _exec_args(pg)
    pg.add_argument("--seed", type=int, default=None, help="reproducible random choice")
    pg.add_argument("--count", type=int, default=1, help="how many monkeys to unleash in a row")
    pg.set_defaults(func=cmd_rampage)

    pw = sub.add_parser("watch", help="run a watcher (probe + assert a signal)")
    pw.add_argument("watcher")
    pw.add_argument("--scope", default="environment")
    pw.add_argument("--execute", action="store_true", help="actually touch the tenant")
    pw.add_argument("--timeout", type=float, default=120.0, help="how long to wait for the signal (s)")
    pw.set_defaults(func=cmd_watch)

    ps = sub.add_parser("restore", help="replay the journal")
    ps.add_argument("--execute", action="store_true")
    ps.set_defaults(func=cmd_restore)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
