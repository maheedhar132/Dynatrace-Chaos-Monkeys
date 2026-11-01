"""The safety guarantees, as tests. If these fail, the tool is not safe to run."""

import pytest

from troop.client import DryRunClient
from troop.journal import Journal
from troop.models import Blast, Change, Plan, Reversibility, Target
from troop.monkeys import FIRE, QUARANTINE
from troop.monkeys.fire import CANARY_TAG, Baboon
from troop.runner import Runner
from troop.safety import (
    PERMANENTLY_DISABLED, Fence, ForbiddenAction, Guard, ScopeViolation, assert_executable,
)


def _guard(dry_run=False, scopes=frozenset({"MZ-CANARY"})):
    return Guard(fence=Fence(allowed_scopes=scopes), dry_run=dry_run)


def _plan(scope="MZ-CANARY", blast=Blast.OBJECT, rev=Reversibility.AUTO, schema="builtin:alerting.profile"):
    ch = Change(Target(schema, "OBJ-1", scope, label=CANARY_TAG), before={"value": {}},
                after={"value": {"x": 1}})
    return Plan("baboon", "test", rev, blast, [ch])


def test_quarantine_names_are_refused_by_name():
    for name in QUARANTINE:
        g = _guard()
        p = _plan()
        p.monkey = name
        with pytest.raises(ForbiddenAction):
            g.vet(p)


def test_every_quarantine_monkey_is_permanently_disabled():
    # the quarantine registry and the off-switch must not drift apart
    for name in QUARANTINE:
        assert name in PERMANENTLY_DISABLED


@pytest.mark.parametrize("name", sorted(PERMANENTLY_DISABLED) + ["super-kong", "KONG", "reaper2"])
def test_assert_executable_refuses_disabled(name):
    with pytest.raises(ForbiddenAction):
        assert_executable(name)


def test_disabled_monkey_cannot_run_even_with_valid_plan(tmp_path):
    # hand-build an otherwise-valid plan but label it with a disabled name
    p = _plan()
    p.monkey = "kong"
    runner = Runner(DryRunClient(), _guard(dry_run=False), Journal(str(tmp_path / "j.jsonl")))
    with pytest.raises(ForbiddenAction):
        runner.run(p)


def test_no_arming_flag_exists_on_guard():
    # there must be no field that could switch a disabled monkey on
    fields = Guard.__dataclass_fields__
    assert not any(k in fields for k in ("arm", "armed", "enable_destructive", "force"))


def test_out_of_fence_scope_refused():
    with pytest.raises(ScopeViolation):
        _guard().vet(_plan(scope="environment"))


def test_wide_blast_refused():
    with pytest.raises(ForbiddenAction):
        _guard().vet(_plan(blast=Blast.FLEET))


def test_irreversible_refused():
    with pytest.raises(ForbiddenAction):
        _guard().vet(_plan(rev=Reversibility.NONE))


def test_denylisted_schema_refused():
    with pytest.raises(ForbiddenAction):
        _guard().vet(_plan(schema="builtin:preferences.privacy"))


def test_kill_switch_halts(tmp_path):
    ks = tmp_path / ".stop"
    ks.write_text("x")
    g = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"}), kill_switch_path=str(ks)))
    with pytest.raises(Exception):
        g.vet(_plan())


def test_dry_run_writes_nothing(tmp_path):
    client = DryRunClient()
    g = _guard(dry_run=True)
    runner = Runner(client, g, Journal(str(tmp_path / "j.jsonl")))
    result = runner.run(_plan())
    assert result.executed is False
    assert client.writes == []


def test_journal_round_trip(tmp_path):
    client = DryRunClient(store={"OBJ-1": {"objectId": "OBJ-1", "schemaId": "s", "value": {"old": 1}}})
    j = Journal(str(tmp_path / "j.jsonl"))
    ch = Change(Target("s", "OBJ-1", "MZ-CANARY"), before={"value": {"old": 1}},
                after={"value": {"new": 2}})
    j.record(ch)
    assert j.pending() == 1
    j.restore(client)
    assert j.pending() == 0
    assert any("OBJ-1" in w for w in client.writes)


def test_token_revoke_round_trip(tmp_path):
    from troop.models import Target
    client = DryRunClient()
    tid = client.create_token("chaos-canary ingest", ["metrics.ingest"])
    client.writes.clear()
    ch = Change(Target("api-token", tid, "canary-token", label=CANARY_TAG, kind="token"),
                before={"name": "chaos-canary ingest", "scopes": ["metrics.ingest"]}, after=None)
    j = Journal(str(tmp_path / "j.jsonl"))
    j.record(ch)
    from troop.resources import apply_change
    apply_change(client, ch)                      # revoke
    assert any("REVOKE" in w for w in client.writes)
    j.restore(client)                             # undo -> recreate
    assert any("CREATE token" in w for w in client.writes)
    assert j.pending() == 0


def test_synthetic_disable_round_trip(tmp_path):
    from troop.models import Target
    from troop.resources import apply_change
    client = DryRunClient()
    client.monitors["SYNTH-1"] = {"entityId": "SYNTH-1", "name": "chaos-canary mon", "enabled": True}
    ch = Change(Target("synthetic-monitor", "SYNTH-1", "canary-synthetic", label=CANARY_TAG, kind="synthetic"),
                before={"value": dict(client.monitors["SYNTH-1"])},
                after={"value": {**client.monitors["SYNTH-1"], "enabled": False}})
    j = Journal(str(tmp_path / "j.jsonl"))
    j.record(ch)
    apply_change(client, ch)
    assert client.monitors["SYNTH-1"]["value"]["enabled"] is False
    j.restore(client)
    assert client.monitors["SYNTH-1"]["value"]["enabled"] is True
    assert j.pending() == 0


def test_tarsier_and_vervet_plan_from_seed():
    c = DryRunClient()
    c.create_token("chaos-canary ingest", ["metrics.ingest"])
    c.monitors["SYNTH-1"] = {"entityId": "SYNTH-1", "name": "chaos-canary mon", "enabled": True}
    tp = FIRE["tarsier"]().plan(c, "canary-token")
    vp = FIRE["vervet"]().plan(c, "canary-synthetic")
    assert len(tp.changes) == 1 and tp.changes[0].target.kind == "token"
    assert len(vp.changes) == 1 and vp.changes[0].target.kind == "synthetic"


def test_every_fire_monkey_is_within_limits():
    for name, cls in FIRE.items():
        m = cls()
        assert m.reversibility <= Reversibility.MANUAL
        assert m.blast <= Blast.SCOPE


def test_potto_reports_blind_spot_when_no_audit(tmp_path):
    from troop.monkeys.watch import Potto
    client = DryRunClient()  # audit_events returns [] -> blind spot
    guard = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"})), dry_run=False)
    result = Potto().run(client, guard, Journal(str(tmp_path / "j.jsonl")),
                         "MZ-CANARY", timeout_s=0, poll_s=0)
    assert result.executed and result.killed is True        # gap found
    assert result.reverted and result.revert_verified       # probe cleaned up


def test_potto_observable_when_audited(tmp_path):
    from troop.monkeys.watch import Potto
    client = DryRunClient()

    def audited_create(schema_id, scope, value):
        import time
        oid = DryRunClient.create_object(client, schema_id, scope, value)
        client.audit.append({"timestamp": int(time.time() * 1000) + 1,
                             "category": "SETTINGS", "entityId": oid})
        return oid

    client.create_object = audited_create  # type: ignore[assignment]
    guard = Guard(fence=Fence(allowed_scopes=frozenset({"MZ-CANARY"})), dry_run=False)
    result = Potto().run(client, guard, Journal(str(tmp_path / "j.jsonl")),
                         "MZ-CANARY", timeout_s=0, poll_s=0)
    assert result.killed is False                            # signal was seen


def test_potto_cannot_run_quarantined_name():
    # the watcher path also honours the permanent off-switch
    from troop.monkeys.watch import Potto
    p = Potto()
    p.name = "kong"
    with pytest.raises(ForbiddenAction):
        p.run(DryRunClient(), _guard(dry_run=False), Journal(":x:"), "MZ-CANARY")
