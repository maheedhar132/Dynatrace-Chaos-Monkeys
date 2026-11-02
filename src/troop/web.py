"""The Swagger UI / HTTP way to run the troop.

Same engine as the CLI, different front door. Start it with:

    pip install .[web]
    troop-web                      # serves on http://127.0.0.1:8077
    # open http://127.0.0.1:8077/docs  <- the Swagger UI

Every endpoint maps to a CLI verb: GET /troop (list), GET /tenants, POST /plan,
POST /run, POST /rampage, POST /watch, POST /restore. The same guard, canary
fence, write-ahead journal and pre-removal backups apply — `execute=false`
(the default) keeps everything in dry-run, exactly like leaving off `--execute`.

FastAPI is imported lazily so the core package and its tests never need it.
"""

# NB: no `from __future__ import annotations` here on purpose — FastAPI must see
# the Pydantic request models as real classes, not stringized annotations.

import os
from typing import Any, Optional

from .backup import BackupVault
from .canary import CanaryKit, CanaryManifest
from .client import DryRunClient, DynatraceClient, HttpDynatraceClient
from .config import Config, Tenant, load_config
from .coverage import classify, fault_note, verify_schemas
from .driver import drive as drive_faults
from .journal import Journal
from .models import ExperimentResult, Fault, Outcome, Plan, new_run_id
from .monkeys import FIRE, QUARANTINE, WATCHERS
from .rampage import choose as rampage_choose
from .report import ResultStore, result_row, summarize, to_markdown
from .runner import Runner
from .selftest import run_selftest
from .safety import Fence, Guard, SafetyError, assert_executable

CONFIG_ENV = "TROOP_CONFIG"


# --- shared wiring (mirrors cli.py, kept dependency-free) -------------------

def _client_for(tenant: Optional[Tenant], dry_run: bool) -> DynatraceClient:
    if dry_run or tenant is None or not tenant.live:
        return DryRunClient()
    return HttpDynatraceClient(tenant.base_url, tenant.resolve_token())


def _guard_for(cfg: Config, tenant: Optional[Tenant], dry_run: bool) -> Guard:
    return Guard(fence=Fence(
        allowed_scopes=tenant.canary_scopes if tenant else frozenset(),
        required_tag=tenant.required_tag if tenant else "chaos-canary",
        kill_switch_path=cfg.kill_switch_path,
        max_ttl_seconds=cfg.max_ttl_seconds,
    ), dry_run=dry_run)


def _plan_dict(plan: Plan) -> dict[str, Any]:
    return {
        "monkey": plan.monkey,
        "description": plan.description,
        "reversibility": str(plan.reversibility),
        "blast": str(plan.blast),
        "fault": plan.fault,
        "expected_signal": plan.expected_signal,
        "changes": [{"target": str(c.target), "note": c.note,
                     "removes": c.after is None} for c in plan.changes],
    }


_KILLED_CAUTION = ("KILLED is a v0 signal, not proof — a merged baseline problem can read as "
                   "KILLED when detection is fine. Verify by hand. Do NOT wire KILLED into "
                   "automated remediation, paging, or config deletion yet.")


def _result_dict(result: ExperimentResult) -> dict[str, Any]:
    if result.outcome is not None:
        verdict = result.outcome.name
    elif result.killed is not None:
        verdict = "KILLED" if result.killed else "SURVIVED"
    else:
        verdict = None
    out = {
        "monkey": result.monkey,
        "run_id": result.run_id,
        "executed": result.executed,
        "detected": result.detected,
        "killed": result.killed,
        "outcome": None if result.outcome is None else result.outcome.name,
        "verdict": verdict,
        "reverted": result.reverted,
        "revert_verified": result.revert_verified,
        "messages": result.messages,
    }
    if result.outcome is Outcome.KILLED:
        out["caution"] = _KILLED_CAUTION
    return out


def create_app(config_path: Optional[str] = None):
    """Build the FastAPI app. Raises a clear error if the web extra is missing."""
    try:
        from fastapi import Body, FastAPI, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install the 'web' extra: pip install .[web]") from exc

    config_path = config_path or os.environ.get(CONFIG_ENV) or "chaos.yaml"

    def cfg() -> Config:
        return load_config(config_path)

    def tenant_or_400(c: Config, tenant_id: Optional[str]) -> Optional[Tenant]:
        try:
            t = c.tenant(tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if t is None and c.tenants and tenant_id is None:
            raise HTTPException(status_code=400,
                                detail=f"several tenants; pass tenant (one of {sorted(c.tenants)})")
        return t

    app = FastAPI(
        title="Dynatrace Chaos Troop",
        description=__doc__,
        version="0.1.0",
    )

    class RunBody(BaseModel):
        monkey: str
        scope: str = "environment"
        tenant: Optional[str] = None
        execute: bool = Field(False, description="false = dry-run (nothing is touched)")
        ttl: int = 300
        oracle_wait: float = Field(0.0, description="seconds to wait for detection (Davis window)")
        poll: float = 2.0
        backup: bool = Field(True, description="take a full backup before any removal")

    class RampageBody(BaseModel):
        scope: str = "environment"
        tenant: Optional[str] = None
        execute: bool = False
        ttl: int = 300
        oracle_wait: float = 0.0
        poll: float = 2.0
        seed: Optional[int] = None
        count: int = 1
        backup: bool = True

    class WatchBody(BaseModel):
        watcher: str
        scope: str = "environment"
        tenant: Optional[str] = None
        execute: bool = False
        timeout: float = 120.0

    class RestoreBody(BaseModel):
        tenant: Optional[str] = None
        execute: bool = False

    class CanaryBody(BaseModel):
        action: str = Field("status", description="setup | status | teardown")
        tenant: Optional[str] = None
        execute: bool = Field(False, description="false = dry-run (nothing is created/deleted)")

    def _run_named(name: str, c: Config, tenant: Optional[Tenant], scope: str,
                   dry: bool, ttl: int, backup: bool,
                   oracle_wait: float = 0.0, poll: float = 2.0) -> dict[str, Any]:
        try:
            assert_executable(name)
        except SafetyError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        if name in QUARANTINE:
            raise HTTPException(status_code=403, detail=f"{name} is quarantined")
        if name not in FIRE:
            raise HTTPException(status_code=404, detail=f"unknown monkey {name!r}")
        client = _client_for(tenant, dry)
        guard = _guard_for(c, tenant, dry)
        vault = BackupVault(c.backup_dir) if backup else None
        fault = getattr(FIRE[name], "fault", Fault.EVENT)
        try:
            plan = FIRE[name]().plan(client, scope)
            result = Runner(client, guard, Journal(c.journal_path), vault=vault).run(
                plan, ttl_seconds=ttl, oracle_wait_s=oracle_wait, poll_s=poll, fault_kind=fault)
        except SafetyError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        if not dry:
            try:
                ResultStore(c.results_path).append(
                    result_row(result, plan, scope=scope,
                               tenant=(tenant.id if tenant else "")))
            except OSError:
                pass
        out = _result_dict(result)
        if result.outcome is Outcome.INCONCLUSIVE and not fault.injectable:
            out["gated_note"] = fault_note(fault, name)
        return out

    @app.get("/troop", summary="list the whole troop")
    def troop() -> dict[str, Any]:
        return {
            "fire": {c.name: {"reversibility": str(FIRE[c.name]().reversibility),
                              "blast": str(FIRE[c.name]().blast),
                              "fault": str(c.fault), "drive": str(c.drive), "live": c.live,
                              "schemas": list(c.schemas)}
                     for c in classify()},
            "live_now": [c.name for c in classify() if c.live],
            "gated": [c.name for c in classify() if not c.live],
            "watchers": list(WATCHERS),
            "quarantine": {n: q.what for n, q in QUARANTINE.items()},
        }

    @app.get("/tenants", summary="configured tenants")
    def tenants() -> dict[str, Any]:
        c = cfg()
        return {tid: {"base_url": t.base_url, "live": t.live,
                      "canary_scopes": sorted(t.canary_scopes), "required_tag": t.required_tag}
                for tid, t in c.tenants.items()}

    @app.post("/canary", summary="provision / inspect / remove the canary fixtures")
    def canary(body: CanaryBody) -> dict[str, Any]:
        if body.action not in ("setup", "status", "teardown"):
            raise HTTPException(status_code=400,
                                detail="action must be setup, status, or teardown")
        c = cfg()
        t = tenant_or_400(c, body.tenant)
        dry = not body.execute
        client = _client_for(t, dry_run=True) if body.action == "status" else _client_for(t, dry)
        kit = CanaryKit(client, manifest_path=c.canary_manifest_path, dry_run=dry)
        lines = {"setup": kit.setup, "status": kit.status, "teardown": kit.teardown}[body.action]()
        return {"action": body.action, "execute": body.execute,
                "canary_scope": kit.manifest.mz_scope, "lines": lines}

    @app.post("/drive", summary="inject faults on demand (event/metric/log)")
    def drive(kind: str = Body("event", embed=True), scope: str = Body("environment", embed=True),
              count: int = Body(1, embed=True), tenant: Optional[str] = Body(None, embed=True),
              execute: bool = Body(False, embed=True)) -> dict[str, Any]:
        try:
            fault = Fault[kind.upper()]
        except KeyError:
            raise HTTPException(status_code=400, detail="kind must be event, metric, or log")
        c = cfg()
        t = tenant_or_400(c, tenant)
        if not execute:
            return {"dry_run": True, "would_inject": count, "kind": kind, "scope": scope}
        sent, log = drive_faults(_client_for(t, dry_run=False), fault, scope,
                                 run_id=new_run_id(), monkey="manual", count=count)
        return {"injected": sent, "kind": kind, "scope": scope, "log": log}

    @app.post("/selftest", summary="prove the live detectors actually fire (end-to-end)")
    def selftest(tenant: Optional[str] = Body(None, embed=True),
                 scope: Optional[str] = Body(None, embed=True),
                 oracle_wait: float = Body(120.0, embed=True),
                 poll: float = Body(5.0, embed=True)) -> dict[str, Any]:
        c = cfg()
        t = tenant_or_400(c, tenant)
        live = t is not None and t.live
        sc = scope or CanaryManifest.load(c.canary_manifest_path).mz_scope
        if not sc:
            raise HTTPException(status_code=400,
                                detail="no canary scope; run canary setup or pass scope")
        checks = run_selftest(_client_for(t, dry_run=not live), sc,
                              oracle_wait=oracle_wait, poll=poll)
        fired = sum(1 for ch in checks if ch.fired)
        return {
            "live_tenant": live, "scope": sc, "fired": fired, "total": len(checks),
            "checks": [{"monkey": ch.monkey, "fault": str(ch.fault),
                        "fired": ch.fired, "detail": ch.detail} for ch in checks],
            "note": None if live else "dry-run: no live tenant, so nothing actually fired",
        }

    @app.get("/report", summary="mutation-score scorecard across runs")
    def report() -> dict[str, Any]:
        c = cfg()
        summary = summarize(ResultStore(c.results_path).rows())
        summary["markdown"] = to_markdown(summary)
        return summary

    @app.post("/verify", summary="prove each monkey's schema against the tenant (read-only)")
    def verify(tenant: Optional[str] = Body(None, embed=True),
               scope: str = Body("environment", embed=True)) -> dict[str, Any]:
        c = cfg()
        t = tenant_or_400(c, tenant)
        client = _client_for(t, dry_run=True) if (t is None or not t.live) \
            else HttpDynatraceClient(t.base_url, t.resolve_token())
        live_tenant = t is not None and t.live
        checks = verify_schemas(client, scope=scope)
        return {
            "live_tenant": live_tenant,
            "note": None if live_tenant else
            "dry-run: schemas NOT checked against Dynatrace; pass a live tenant to prove them",
            "checks": [{"monkey": ch.monkey, "schema": ch.schema_id,
                        "status": ch.status, "ok": ch.ok, "detail": ch.detail}
                       for ch in checks],
            "issues": [{"monkey": ch.monkey, "schema": ch.schema_id, "detail": ch.detail}
                       for ch in checks if not ch.ok],
        }

    @app.post("/plan", summary="dry-run a monkey (never touches a tenant)")
    def plan(monkey: str = Body(..., embed=True), scope: str = Body("environment", embed=True),
             tenant: Optional[str] = Body(None, embed=True)) -> dict[str, Any]:
        if monkey in QUARANTINE:
            raise HTTPException(status_code=403, detail=f"{monkey} is quarantined")
        if monkey not in FIRE:
            raise HTTPException(status_code=404, detail=f"unknown monkey {monkey!r}")
        c = cfg()
        t = tenant_or_400(c, tenant)
        out = _plan_dict(FIRE[monkey]().plan(_client_for(t, dry_run=True), scope))
        fault = getattr(FIRE[monkey], "fault", Fault.EVENT)
        out["fault"] = str(fault)
        out["live"] = fault.injectable
        note = fault_note(fault, monkey)
        if note:
            out["gated_note"] = note
        return out

    @app.post("/run", summary="run one monkey (dry-run unless execute=true)")
    def run(body: RunBody) -> dict[str, Any]:
        c = cfg()
        t = tenant_or_400(c, body.tenant)
        return _run_named(body.monkey, c, t, body.scope, not body.execute, body.ttl,
                          body.backup, body.oracle_wait, body.poll)

    @app.post("/rampage", summary="let the troop pick a monkey at random (choice is logged)")
    def rampage(body: RampageBody) -> dict[str, Any]:
        c = cfg()
        t = tenant_or_400(c, body.tenant)
        dry = not body.execute
        client = _client_for(t, dry)
        runs: list[dict[str, Any]] = []
        log: list[str] = []
        for i in range(max(1, body.count)):
            seed = None if body.seed is None else body.seed + i
            name, lines = rampage_choose(client, body.scope, seed=seed)
            log.extend(lines)
            if name is None:
                break
            runs.append(_run_named(name, c, t, body.scope, dry, body.ttl,
                                   body.backup, body.oracle_wait, body.poll))
        return {"log": log, "runs": runs}

    @app.post("/watch", summary="run a watcher (probe + assert a signal)")
    def watch(body: WatchBody) -> dict[str, Any]:
        if body.watcher not in WATCHERS:
            raise HTTPException(status_code=404, detail=f"unknown watcher {body.watcher!r}")
        c = cfg()
        t = tenant_or_400(c, body.tenant)
        dry = not body.execute
        try:
            result = WATCHERS[body.watcher]().run(
                _client_for(t, dry), _guard_for(c, t, dry), Journal(c.journal_path),
                body.scope, timeout_s=body.timeout)
        except SafetyError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        out = _result_dict(result)
        out["verdict"] = None if result.killed is None else ("BLIND SPOT" if result.killed else "OBSERVABLE")
        return out

    @app.post("/restore", summary="replay the journal and put everything back")
    def restore(body: RestoreBody) -> dict[str, Any]:
        c = cfg()
        t = tenant_or_400(c, body.tenant)
        journal = Journal(c.journal_path)
        if journal.pending() == 0:
            return {"restored": [], "pending": 0}
        undone = journal.restore(_client_for(t, dry_run=not body.execute))
        return {"restored": undone, "pending": journal.pending()}

    return app


def main() -> int:
    """Console entry point: serve the Swagger UI with uvicorn."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("install the 'web' extra: pip install .[web]")
        return 1
    host = os.environ.get("TROOP_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("TROOP_WEB_PORT", "8077"))
    print(f"troop web UI: http://{host}:{port}/docs")
    uvicorn.run(create_app(), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
