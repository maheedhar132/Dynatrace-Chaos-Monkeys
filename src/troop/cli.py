"""`troop` command line.

    troop list                       show the whole troop and verdicts
    troop tenants                    show configured tenants
    troop canary  setup --execute    provision the canary fixtures a run needs
    troop canary  status             show the provisioned canary
    troop canary  teardown --execute remove the canary fixtures (exact, from manifest)
    troop quickstart [--tenant]      the ordered path from zero to your first verdict
    troop verify  [--tenant]         prove each monkey's schema against the tenant (read-only)
    troop selftest [--tenant]        prove the live detectors actually fire (end-to-end smoke)
    troop drive   --kind K --scope S inject faults on demand (event/metric/log)
    troop plan    <monkey> [--scope] dry-run: what it would do (no tenant needed)
    troop run     <monkey> --execute real run (needs config); dry-run without --execute
    troop rampage [--seed]           let the troop pick a monkey at random (logged)
    troop report  [--md f] [--json f]  mutation-score scorecard across runs
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
from contextlib import nullcontext
from typing import Optional

from .backup import BackupVault
from .canary import CanaryKit, CanaryManifest
from .client import DryRunClient, DynatraceClient, HttpDynatraceClient
from .config import Config, Tenant, load_config
from .coverage import classify, fault_note, verify_schemas
from .driver import TraceLoad, drive as drive_faults
from .journal import Journal
from .models import Fault, Outcome, new_run_id
from .monkeys import FIRE, QUARANTINE, WATCHERS
from .preflight import run_checks, worst
from .rampage import choose as rampage_choose
from .report import (ResultStore, filter_since, parse_duration, result_row,
                     summarize, to_json, to_markdown, trend_markdown)
from .runner import Runner
from .selftest import run_selftest
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
    print(f"  {'':7s} {'name':10s} {'revert':6s} {'blast':7s} {'fault':7s} driven by")
    how = {Fault.EVENT: "an ingested error event (ready now)",
           Fault.METRIC: "an ingested canary metric (ready now)",
           Fault.LOG: "an ingested canary log line (ready now)",
           Fault.TRACE: "real service traffic (run the reference canary)",
           Fault.NONE: "an out-of-band fault (token/agent/synthetic)"}
    for c in classify():
        inst = FIRE[c.name]()
        tag = "LIVE" if c.live else "GATED"
        print(f"  [{tag:5s}] {c.name:10s} {inst.reversibility!s:6s} {inst.blast!s:7s} "
              f"{str(c.fault):7s} {how[c.fault]}")
    live = sum(1 for c in classify() if c.live)
    print(f"\n  {live} live (the driver injects their fault now), {len(FIRE) - live} gated "
          f"(need real traffic / an out-of-band fault). `troop drive` injects faults on demand; "
          f"`troop verify` proves schemas on your tenant.")
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


def cmd_canary(args) -> int:
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    dry = not getattr(args, "execute", False)
    if args.action == "status":
        client = _readonly_client_for(tenant)        # status never writes
    else:
        client = _client_for(tenant, dry_run=dry)
    kit = CanaryKit(client, manifest_path=cfg.canary_manifest_path, dry_run=dry)
    actions = {"setup": kit.setup, "teardown": kit.teardown, "status": kit.status}
    for line in actions[args.action]():
        print(line)
    return 0


def cmd_selftest(args) -> int:
    """Prove the live faults actually open a problem on this tenant, end to end."""
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    live = tenant is not None and tenant.live
    client = _client_for(tenant, dry_run=not live)

    kit: Optional[CanaryKit] = None
    scope = args.scope
    if args.setup:
        kit = CanaryKit(client, manifest_path=cfg.canary_manifest_path, dry_run=not live)
        for line in kit.setup():
            print(line)
        scope = kit.manifest.mz_scope
    if not scope:
        scope = CanaryManifest.load(cfg.canary_manifest_path).mz_scope
    if not scope:
        print("[STOP] no canary scope. Run `troop canary setup --execute` first, pass --scope, "
              "or add --setup to provision one here.", file=sys.stderr)
        return 2

    print(f"\nself-test on scope {scope!r} (injecting each live fault, watching for a problem):")
    # No live tenant means no problems ever come back, so don't wait the window.
    wait = args.oracle_wait if live else 0.0
    checks = run_selftest(client, scope, oracle_wait=wait, poll=args.poll)
    for c in checks:
        tag = "[ OK ]" if c.fired else "[FAIL]"
        print(f"  {tag} {c.monkey:10s} {str(c.fault):7s} {c.detail}")
    fired = sum(1 for c in checks if c.fired)
    print(f"\n{fired}/{len(checks)} live detectors fired.")

    if args.teardown and kit is not None:
        print()
        for line in kit.teardown():
            print(line)

    if not live:
        print("\n(dry-run: no live tenant selected, so nothing actually fired. Point --tenant at a "
              "live tenant to really self-test.)")
        return 0
    return 0 if fired == len(checks) else 2


def cmd_quickstart(args) -> int:
    """Print the exact next command for where this setup is, start to first verdict."""
    cfg = load_config(args.config)
    tenant = cfg.tenant(getattr(args, "tenant", None)) if cfg.tenants else None
    tname = tenant.id if tenant else "<tenant-id>"
    scope = CanaryManifest.load(cfg.canary_manifest_path).mz_scope
    tenant_flag = f" --tenant {tname}" if cfg.tenants else ""

    def step(done: bool, text: str, *cmds: str) -> None:
        print(f"  [{'x' if done else ' '}] {text}")
        for c in cmds:
            print(f"        {c}")

    print("troop quickstart - from zero to your first real verdict:\n")
    step(bool(cfg.tenants), "1. configure a tenant in chaos.yaml (copy chaos.example.yaml)",
         "cp chaos.example.yaml chaos.yaml   # then set base_url + token_env + export the token")
    step(bool(scope), "2. provision the canary (zone + mutation targets + matching detectors)",
         f"troop canary setup{tenant_flag} --execute")
    step(False, "3. prove the schemas fit your tenant (read-only, never writes)",
         f"troop verify{tenant_flag}")
    step(False, "4. prove the fence is safe",
         f"troop doctor{tenant_flag}")
    step(False, "5. prove the live detectors actually fire (end-to-end smoke)",
         f"troop selftest{tenant_flag}")
    step(False, "6. run a live monkey and get a verdict",
         f"troop run baboon --scope \"{scope or '<canary-scope>'}\"{tenant_flag} "
         f"--execute --oracle-wait 600")
    step(False, "7. a gated (TRACE) monkey: drive the reference canary",
         "python examples/canary_service.py --port 8099   # under OneAgent, tagged chaos-canary",
         f"troop run gibbon --scope \"{scope or '<canary-scope>'}\"{tenant_flag} "
         f"--execute --oracle-wait 600 --drive-canary http://localhost:8099")
    step(False, "8. the scorecard",
         "troop report --md report.md --json report.json --trend")
    if scope:
        print(f"\n  canary scope already provisioned: {scope}")
    print("\n  > v0 oracle: verdicts are for humans to review, not to automate on.")
    return 0


def cmd_drive(args) -> int:
    """Inject faults on demand — the API-side canary workload, and what makes a
    metric/log monkey's baseline fire."""
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    dry = not args.execute
    try:
        fault = Fault[args.kind.upper()]
    except KeyError:
        print(f"[STOP] unknown fault kind {args.kind!r}; use: event, metric, log", file=sys.stderr)
        return 2
    if dry:
        print(f"[dry-run] would inject {args.count} {fault} fault(s) into {args.scope!r}. "
              f"Re-run with --execute against a live tenant.")
        return 0
    client = _client_for(tenant, dry_run=False)
    sent, log = drive_faults(client, fault, args.scope, run_id=new_run_id(),
                             monkey="manual", count=args.count)
    for line in log:
        print(line)
    return 0 if sent else 2


def cmd_report(args) -> int:
    cfg = load_config(args.config)
    store = ResultStore(cfg.results_path)
    if args.clear:
        store.clear()
        print(f"cleared {cfg.results_path}")
        return 0
    rows = store.rows()
    if args.since:
        import time as _t
        rows = filter_since(rows, int(_t.time() * 1000) - parse_duration(args.since) * 1000)
    summary = summarize(rows)
    md = to_markdown(summary)
    if args.trend:
        md += "\n" + trend_markdown(rows)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(to_json(summary))
        print(f"wrote {args.json}")
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"wrote {args.md}")
    if not args.json and not args.md:
        print(md)
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
    fault = getattr(FIRE[args.monkey], "fault", Fault.EVENT)
    print(f"monkey:   {plan.monkey}\nwhat:     {plan.description}")
    print(f"risk:     {plan.reversibility}/{plan.blast}")
    print(f"fault:    {fault} ({'live - the driver injects it now' if fault.injectable else 'gated'})")
    print(f"injected: {plan.fault}\nexpected: {plan.expected_signal}")
    print(f"changes:  {len(plan.changes)}")
    for ch in plan.changes:
        print(f"  - {ch.target}: {ch.note}")
    if not plan.changes:
        print("  (none - no canary-tagged objects found in this scope)")
    note = fault_note(fault, args.monkey)
    if note:
        print(f"\n[note] {note}")
    return 0


def cmd_verify(args) -> int:
    cfg = load_config(args.config)
    tenant = _resolve_tenant(cfg, args)
    client = _readonly_client_for(tenant)             # validate-only; never writes
    live_tenant = tenant is not None and tenant.live
    checks = verify_schemas(client, scope=args.scope)
    mark = {"valid": "[ OK ]", "present": "[ OK ]", "n/a": "[ -- ]",
            "missing": "[FAIL]", "invalid": "[FAIL]"}
    for c in checks:
        print(f"  {mark.get(c.status, '[????]')} {c.monkey:10s} {c.schema_id:48s} "
              f"{c.status:8s} {c.detail}")
    bad = [c for c in checks if not c.ok]
    if not live_tenant:
        print("\n(dry-run: no live tenant selected, so schemas were NOT checked against "
              "Dynatrace. Run `troop verify --tenant <live-id>` to prove them for real.)")
    elif bad:
        print(f"\n{len(bad)} schema issue(s) above — those monkeys need their shape adjusted "
              f"for your tenant/version before they'll run.")
    else:
        print("\nall schemas present and accepted on this tenant.")
    return 2 if (live_tenant and bad) else 0


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
    fault = getattr(FIRE.get(name), "fault", Fault.EVENT)
    gated_note = fault_note(fault, name)
    if not dry and gated_note:
        print(f"[warn] {gated_note}", file=sys.stderr)
    client = _client_for(tenant, dry_run=dry)
    guard = _guard_for(cfg, tenant, dry_run=dry)
    runner = Runner(client, guard, _journal_for(cfg, args), vault=_vault_for(cfg, args))
    # For a TRACE monkey, --drive-canary generates real load against the reference
    # service for the duration of the run so the baseline has traffic to detect.
    canary_url = getattr(args, "drive_canary", None)
    load = (TraceLoad(canary_url) if (not dry and canary_url and fault is Fault.TRACE)
            else nullcontext())
    if not dry and canary_url and fault is Fault.TRACE:
        print(f"[drive-canary] generating load against {canary_url} for this run", file=sys.stderr)
    try:
        plan = FIRE[name]().plan(client, args.scope)
        with load:
            result = runner.run(plan, ttl_seconds=args.ttl,
                                oracle_wait_s=args.oracle_wait, poll_s=args.poll,
                                fault_kind=fault)
    except SafetyError as exc:
        print(f"[STOP] guard refused: {exc}", file=sys.stderr)
        return 2
    if not dry:   # persist executed runs for `troop report`
        try:
            ResultStore(cfg.results_path).append(
                result_row(result, plan, scope=args.scope,
                           tenant=(tenant.id if tenant else "")))
        except OSError:
            pass
    for m in result.messages:
        print(m)
    if result.outcome is not None:
        print(f"\nverdict: {result.outcome}")
        if result.outcome is Outcome.KILLED:
            print("[CAUTION] KILLED is a v0 signal, not proof. A merged baseline problem can "
                  "read as KILLED when detection is actually fine. Verify by hand. DO NOT wire "
                  "KILLED into automated remediation, paging, or config deletion yet.",
                  file=sys.stderr)
        elif result.outcome is Outcome.INCONCLUSIVE and gated_note:
            print(f"[note] INCONCLUSIVE is expected here — {gated_note}", file=sys.stderr)
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
        name, log = rampage_choose(client, args.scope, seed=seed,
                                   live_only=not getattr(args, "include_gated", False))
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

    pc = sub.add_parser("canary", help="provision / inspect / remove the canary fixtures")
    pc.add_argument("action", choices=["setup", "status", "teardown"],
                    help="setup provisions, status shows, teardown removes (from the manifest)")
    pc.add_argument("--execute", action="store_true",
                    help="actually create/delete on the tenant (dry-run without it)")
    pc.set_defaults(func=cmd_canary)

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
        sp.add_argument("--drive-canary", dest="drive_canary", default=None,
                        help="for a TRACE monkey: URL of the reference canary service to drive "
                             "(e.g. http://localhost:8099) so the run has real traffic")

    pr = sub.add_parser("run", help="run a monkey (dry-run unless --execute)")
    pr.add_argument("monkey")
    _exec_args(pr)
    pr.set_defaults(func=cmd_run)

    pg = sub.add_parser("rampage", help="let the troop pick a monkey at random (logged)")
    _exec_args(pg)
    pg.add_argument("--seed", type=int, default=None, help="reproducible random choice")
    pg.add_argument("--count", type=int, default=1, help="how many monkeys to unleash in a row")
    pg.add_argument("--include-gated", dest="include_gated", action="store_true",
                    help="also pick traffic/external-gated monkeys (they'll likely be INCONCLUSIVE)")
    pg.set_defaults(func=cmd_rampage)

    sub.add_parser("quickstart", help="the ordered path from zero to your first verdict"
                   ).set_defaults(func=cmd_quickstart)

    pv = sub.add_parser("verify", help="prove each monkey's schema against the tenant (read-only)")
    pv.add_argument("--scope", default="environment",
                    help="scope for validate-only checks (default: environment)")
    pv.set_defaults(func=cmd_verify)

    pst = sub.add_parser("selftest", help="prove the live detectors actually fire (end-to-end)")
    pst.add_argument("--scope", default=None, help="canary scope (default: from the manifest)")
    pst.add_argument("--setup", action="store_true", help="provision the canary first")
    pst.add_argument("--teardown", action="store_true", help="remove the canary after (needs --setup)")
    pst.add_argument("--oracle-wait", dest="oracle_wait", type=float, default=120.0,
                     help="seconds to wait for a problem (set to your Davis window)")
    pst.add_argument("--poll", type=float, default=5.0, help="poll interval (s)")
    pst.set_defaults(func=cmd_selftest)

    pd = sub.add_parser("drive", help="inject faults on demand (event/metric/log)")
    pd.add_argument("--kind", default="event", help="event | metric | log")
    pd.add_argument("--scope", default="environment")
    pd.add_argument("--count", type=int, default=1, help="how many faults to inject")
    pd.add_argument("--execute", action="store_true", help="actually ingest (dry-run without it)")
    pd.set_defaults(func=cmd_drive)

    prp = sub.add_parser("report", help="mutation-score scorecard across runs")
    prp.add_argument("--md", default=None, help="write the Markdown report to this path")
    prp.add_argument("--json", default=None, help="write the JSON report to this path")
    prp.add_argument("--since", default=None, help="only runs newer than this (e.g. 7d, 24h, 90m)")
    prp.add_argument("--trend", action="store_true", help="add the mutation-score-by-day table")
    prp.add_argument("--clear", action="store_true", help="clear the stored results")
    prp.set_defaults(func=cmd_report)

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
