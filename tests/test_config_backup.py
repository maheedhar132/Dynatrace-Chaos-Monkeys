"""Multi-tenant config, pre-removal backups, and randomized rampage."""

import json
import os

from troop.backup import BackupVault
from troop.client import DryRunClient
from troop.config import Tenant, parse_config
from troop.journal import Journal
from troop.models import Blast, Change, Plan, Reversibility, Target
from troop.monkeys.fire import CANARY_TAG
from troop.rampage import choose, eligible_monkeys
from troop.runner import Runner
from troop.safety import PERMANENTLY_DISABLED, Fence, Guard


# --- config -----------------------------------------------------------------

def test_multi_tenant_parse_and_select():
    cfg = parse_config({
        "tenants": [
            {"id": "a", "base_url": "https://a.dt", "token_env": "TA", "canary_scopes": ["MZ-A"]},
            {"id": "b", "base_url": "https://b.dt", "token_env": "TB", "canary_scopes": ["MZ-B"]},
        ],
        "default_tenant": "a",
    })
    assert set(cfg.tenants) == {"a", "b"}
    assert cfg.tenant(None).id == "a"                 # falls back to default
    assert cfg.tenant("b").canary_scopes == frozenset({"MZ-B"})


def test_ambiguous_tenant_is_none_without_default():
    cfg = parse_config({"tenants": [
        {"id": "a", "base_url": "x"}, {"id": "b", "base_url": "y"},
    ]})
    assert cfg.tenant(None) is None                   # caller must ask for one


def test_legacy_single_tenant_shape():
    cfg = parse_config({"base_url": "https://only.dt", "canary_scopes": ["MZ-X"]})
    assert list(cfg.tenants) == ["default"]
    assert cfg.tenant(None).base_url == "https://only.dt"


def test_token_env_wins_over_inline(monkeypatch):
    t = Tenant(id="a", base_url="x", token_env="MY_TOK", token="inline")
    assert t.resolve_token() == "inline"              # no env var set -> inline
    monkeypatch.setenv("MY_TOK", "from-env")
    assert t.resolve_token() == "from-env"
    assert t.live is True


# --- pre-removal backup -----------------------------------------------------

def test_vault_backs_up_before_a_removal(tmp_path):
    client = DryRunClient(store={"OBJ-1": {"objectId": "OBJ-1", "schemaId": "s", "value": {"x": 1}}})
    ch = Change(Target("s", "OBJ-1", "MZ-CANARY", label=CANARY_TAG),
                before={"value": {"x": 1}}, after=None)          # a delete
    vault = BackupVault(str(tmp_path / "bk"))
    path = vault.backup(client, ch, "run-1")
    assert path and os.path.exists(path)
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["reason"] == "delete/revoke"
    assert data["live_snapshot"]["objectId"] == "OBJ-1"          # full live state captured
    assert data["target"]["object_id"] == "OBJ-1"


def test_vault_skips_pure_creates(tmp_path):
    client = DryRunClient()
    ch = Change(Target("s", None, "MZ-CANARY", label=CANARY_TAG), before=None, after={"value": {}})
    assert BackupVault(str(tmp_path / "bk")).backup(client, ch, "run-1") is None


def test_runner_writes_a_backup_before_overwriting(tmp_path):
    class SimClient(DryRunClient):
        def recent_problems(self, since_ms):          # make the baseline pass
            return [{"title": "chaos problem"}]

    client = SimClient(store={"OBJ-1": {"objectId": "OBJ-1", "schemaId": "builtin:alerting.profile",
                                        "value": {"a": 1}}})
    ch = Change(Target("builtin:alerting.profile", "OBJ-1", "MZ-CANARY", label=CANARY_TAG),
                before={"value": {"a": 1}}, after={"value": {"a": 2}})
    plan = Plan("baboon", "overwrite a canary profile", Reversibility.AUTO, Blast.OBJECT, [ch],
                fault="f", expected_signal="e")
    vault = BackupVault(str(tmp_path / "bk"))
    guard = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"})), dry_run=False)
    result = Runner(client, guard, Journal(str(tmp_path / "j.jsonl")), vault=vault).run(plan)
    assert result.executed and result.reverted
    assert len(vault.saved) == 1 and os.path.exists(vault.saved[0])


# --- rampage (randomized) ---------------------------------------------------

def test_eligible_pool_excludes_disabled():
    pool = eligible_monkeys()
    assert pool, "there should be fire monkeys"
    assert not any(any(d in n for d in PERMANENTLY_DISABLED) for n in pool)


def test_choose_is_reproducible_with_seed():
    c = DryRunClient()
    a, _ = choose(c, "environment", seed=7)
    b, _ = choose(c, "environment", seed=7)
    assert a == b and a in eligible_monkeys()


def test_choose_logs_the_decision():
    _, log = choose(DryRunClient(), "environment", seed=1)
    assert any("chose:" in line for line in log)
    assert any("eligible:" in line for line in log)
