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
from .client import DryRunClient, DynatraceClient, HttpDynatraceClient
from .config import Config, Tenant, load_config
from .journal import Journal
from .models import ExperimentResult, Plan
from .monkeys import FIRE, QUARANTINE, WATCHERS
from .rampage import choose as rampage_choose
from .runner import Runner
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


def _result_dict(result: ExperimentResult) -> dict[str, Any]:
    if result.outcome is not None:
        verdict = result.outcome.name
    elif result.killed is not None:
        verdict = "KILLED" if result.killed else "SURVIVED"
    else:
        verdict = None
    return {
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
        try:
            plan = FIRE[name]().plan(client, scope)
            result = Runner(client, guard, Journal(c.journal_path), vault=vault).run(
                plan, ttl_seconds=ttl, oracle_wait_s=oracle_wait, poll_s=poll)
        except SafetyError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        return _result_dict(result)

    @app.get("/troop", summary="list the whole troop")
    def troop() -> dict[str, Any]:
        return {
            "fire": {n: {"reversibility": str(c().reversibility), "blast": str(c().blast)}
                     for n, c in FIRE.items()},
            "watchers": list(WATCHERS),
            "quarantine": {n: q.what for n, q in QUARANTINE.items()},
        }

    @app.get("/tenants", summary="configured tenants")
    def tenants() -> dict[str, Any]:
        c = cfg()
        return {tid: {"base_url": t.base_url, "live": t.live,
                      "canary_scopes": sorted(t.canary_scopes), "required_tag": t.required_tag}
                for tid, t in c.tenants.items()}

    @app.post("/plan", summary="dry-run a monkey (never touches a tenant)")
    def plan(monkey: str = Body(..., embed=True), scope: str = Body("environment", embed=True),
             tenant: Optional[str] = Body(None, embed=True)) -> dict[str, Any]:
        if monkey in QUARANTINE:
            raise HTTPException(status_code=403, detail=f"{monkey} is quarantined")
        if monkey not in FIRE:
            raise HTTPException(status_code=404, detail=f"unknown monkey {monkey!r}")
        c = cfg()
        t = tenant_or_400(c, tenant)
        return _plan_dict(FIRE[monkey]().plan(_client_for(t, dry_run=True), scope))

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
