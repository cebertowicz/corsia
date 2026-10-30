"""Where an account assignment may and may not cross a boundary.

Pins: the account policy's actor/project/destination boundaries, unknown-model admission,
a session that no longer matches its account and a catalog row removed from under a binding."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vendor_account_fixtures import set_legacy_policy

from vendor_account_fixtures import accounts, claude, ready, signed_in_default, THREAD  # noqa: F401


@pytest.fixture
def other_model(accounts):
    db = accounts[0]
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('codex/other','Other','codex/other','codex','other','codex','t','t')")
    db.commit()
    return accounts


def _binding(db, thread_id="destination"):
    row = db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=?", (thread_id,)).fetchone()
    return None if row is None else row[0]


def _hold(db, thread_id="destination"):
    row = db.execute("SELECT reason FROM thread_vendor_account_holds WHERE thread_id=?", (thread_id,)).fetchone()
    return None if row is None else row[0]


def _epoch(db):
    return db.execute("SELECT runtime_context_epoch FROM threads WHERE id='destination'").fetchone()[0]


THREADS = {"codex": "destination", "claude-code": "claude-destination"}


MODELS = {"codex": "codex/test", "claude-code": "claude-code/test"}


PROVIDERS = ["codex", "claude-code"]


class _Untouched:
    """Stands where a refusal must come first; any use is a failure."""

    def __getattr__(self, name):
        raise AssertionError(f"reached .{name} after the catalog row was removed")


class _ThreadOnly(_Untouched):
    """A batch whose only readable field is the thread it was claimed for."""

    def __init__(self, thread_id: str):
        self.thread_id = thread_id


@pytest.fixture
def removed(accounts):
    """Bind each vendor thread, freeze a Turn on its account, then delete the model rows."""
    from connections.account_evidence import mark_discovery
    from connections.account_policy import select_account
    from connections.account_receipts import capture_receipt
    from connections.models import upsert_connection
    from connections.vendor_accounts import list_accounts
    from infra.vendor_turn_binding import turn_account_id
    from llm.resolution import resolve_execution_target

    db, legacy, second, _ = accounts
    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/synthetic"})
    db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES ('claude-destination','user','claude-code/test','destination-project','t','t')")
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('claude-code/test','Test','claude-code/test','claude-code','test','claude-code','t','t')")
    db.commit()
    signed_in_default("claude-code", db=db, models=(MODELS["claude-code"],))
    bound = {}
    for provider in PROVIDERS:
        thread, model = THREADS[provider], MODELS[provider]
        assert resolve_execution_target(model, db=db).harness == provider
        bound[provider] = select_account(thread, provider, db=db)
        assert bound[provider] is not None
        frozen = turn_account_id(thread, model, db=db)
        assert frozen == bound[provider]
        db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
                   "VALUES (?,?,0,?,'running','t','test',?)", (provider + "-turn", thread, model, frozen))
    assert bound["codex"] == legacy["id"]
    # An added account, bound and frozen the same way, for the gate that must keep running.
    select_account("actor", "codex", account_id=second["id"], db=db)
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('added-turn','actor',0,'codex/test','running','t','test',?)", (second["id"],))
    db.execute("DELETE FROM models WHERE id IN ('codex/test','claude-code/test')")
    # An account's discovery lists its models as catalog rows too, so the row is gone only once no account lists it.
    for provider in PROVIDERS:
        for account in list_accounts(provider=provider, db=db):
            if account['state'] == 'signed_in':
                receipt = capture_receipt(provider, account_id=account['id'], db=db)
                assert mark_discovery(account['id'], receipt=receipt, status='ready', models={}, db=db)
    db.commit()
    return SimpleNamespace(db=db, bound=bound, added=second["id"])


def _bindings(db):
    return [tuple(row) for row in db.execute(
        "SELECT thread_id,provider,account_id FROM thread_vendor_accounts ORDER BY thread_id")]


def _missing(model):
    return f"Model '{model}' not found in catalog"


def _launched(session, provider, turn_id):
    """The session as its launch froze it on the Turn's account, which every account, the Default included, has.

    The Turn was claimed while the catalog row existed, so the claim is not repeated here.
    """
    from connections.account_evidence import mark_discovery
    from connections.account_projection import ensure_projection
    from connections.account_receipts import capture_receipt
    from connections.account_runtime import homes_for
    from connections.vendor_accounts import get_account
    from infra.database import get_db

    db = get_db()
    try:
        account = get_account(db.execute("SELECT vendor_account_id FROM active_turns WHERE id=?", (turn_id,)).fetchone()[0], db=db)
        # The model left the catalog table after the claim; the account's own discovery still lists it.
        receipt = capture_receipt(provider, account_id=account["id"], db=db)
        mark_discovery(account["id"], receipt=receipt, status="ready", models={MODELS[provider]: {}}, db=db)
        db.commit()
        homes = homes_for(account)
    finally:
        db.close()
    if provider == "claude-code":
        session.claude_dir = homes.claude_dir
    else:
        session.codex_home, session.source_codex_home = homes.codex_runtime, homes.codex_source
        ensure_projection(receipt, home=homes.codex_runtime, kind="live", owner_turn_id=turn_id)
    session.vendor_account_id, session.vendor_account_receipt = account["id"], receipt
    session.vendor_account_bound, session.vendor_account_custom = True, True
    session.vendor_catalog_model = MODELS[provider]
    return session


def _session(provider, thread, turn_id, writes, *, launched=False):
    from infra.thread_input_types import TurnIdentity
    from subagents.cc_stream import CCStreamSession
    from subagents.codex_app_server import CodexAppServerSession

    if provider == "claude-code":
        session = CCStreamSession(binary="/bin/synthetic", session_id="test", corsia_thread_id=thread)
        session._proc = SimpleNamespace(stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()), returncode=None)
        return _launched(session, provider, turn_id) if launched else session
    session = CodexAppServerSession(binary="/bin/synthetic", task=None, corsia_thread_id=thread,
                                    corsia_turn_identity=TurnIdentity(turn_id, 1))

    def write(data):
        writes.append(data)
        for pending in session._pending.values():
            pending.set_result({"accepted": True})

    session._proc = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()), returncode=None)
    return _launched(session, provider, turn_id) if launched else session


async def _steer(provider, session, observed, accepted):
    if provider == "claude-code":
        return await session.feed("steer", on_write_observed=lambda: observed.append(True),
                                  on_accepted=lambda: accepted.append(True))
    return await session._request("turn/steer", {"input": "steer"}, on_write_observed=lambda: observed.append(True),
                                  on_drain_confirmed=lambda: accepted.append(True))


def _claude_binding(db):
    return db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider='claude-code'",
                      (THREAD,)).fetchone()[0]


async def _feed(session, observed, accepted):
    await session.feed("steer", on_write_observed=lambda: observed.append(True),
                       on_accepted=lambda: accepted.append(True))


def test_order_and_default_survive_persistence_and_existing_binding(accounts):
    from connections.account_policy import select_account
    from connections.provider_policy import effective_policy
    db, legacy, second, third = accounts
    ordered = [third["id"], second["id"], legacy["id"]]
    saved = set_legacy_policy("destination-project", "codex", allowlist=ordered,
                       default_account_id=second["id"], db=db)
    assert saved["order"] == [second["id"], third["id"], legacy["id"]]
    assert effective_policy("destination-project", "codex", db=db) == {key: value for key, value in saved.items() if key != "units"}
    assert select_account("destination", "codex", db=db) == second["id"]
    set_legacy_policy("destination-project", "codex", allowlist=ordered,
               default_account_id=third["id"], db=db)
    assert select_account("destination", "codex", db=db) == second["id"]
    assert db.execute("SELECT model FROM threads WHERE id='destination'").fetchone()[0] == "codex/test"


@pytest.mark.parametrize("denied", ["actor-project", "destination-project"])
def test_actor_and_destination_deny_all_cannot_spoof_cross_project(accounts, denied):
    from connections.account_policy import AccountAdmissionError, select_account
    db, legacy, second, third = accounts
    set_legacy_policy(denied, "codex", allowlist=[], default_account_id=None, db=db)
    with pytest.raises(AccountAdmissionError) as error:
        select_account("destination", "codex", account_id=legacy["id"], actor_thread_id="actor", db=db)
    assert error.value.reason == "account_project_denied"
    assert error.value.alternatives == []
    assert second["name"] not in str(error.value)
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts WHERE thread_id='destination'").fetchone()[0] == 0


def test_unordered_policy_skips_a_denied_default(accounts):
    from connections.account_policy import scope_account
    from connections.provider_policy import ordered_accounts, set_policy

    db, legacy, second, third = accounts
    denied_default = set_policy("destination-project", "codex", rollover=False, order=None,
                                disabled=[legacy["id"]], expected_revision=0, db=db)
    assert denied_default["order"] is None
    assert scope_account("codex", project_id="destination-project", db=db) == (second["id"], None)

    ordered = set_policy("destination-project", "codex", rollover=False, order=[second["id"]],
                         disabled=[legacy["id"], third["id"]], expected_revision=1, db=db)
    assert [account["id"] for account in ordered_accounts("codex", ordered, db=db)] == [second["id"]]
    assert scope_account("codex", project_id="destination-project", db=db) == (second["id"], None)


def test_authorized_cross_project_intersection_retains_destination_order(accounts):
    from connections.account_policy import AccountAdmissionError, select_account
    from connections.provider_policy import set_policy

    db, legacy, second, third = accounts
    set_policy("actor-project", "codex", rollover=False, order=[legacy["id"], second["id"]],
               disabled=[third["id"]], expected_revision=0, db=db)
    set_policy("destination-project", "codex", rollover=False, order=[third["id"], second["id"], legacy["id"]],
               disabled=[], expected_revision=0, db=db)
    assert select_account("destination", "codex", account_id=legacy["id"], db=db) == legacy["id"]
    assert select_account("destination", "codex", actor_thread_id="actor", db=db) == legacy["id"]
    with pytest.raises(AccountAdmissionError) as error:
        select_account("destination", "codex", account_id=third["id"], actor_thread_id="actor", db=db)
    assert [item["id"] for item in error.value.alternatives] == [second["id"], legacy["id"]]
    assert all("source_home" not in item and "discovery" not in item for item in error.value.alternatives)


def test_custom_account_never_executes_using_default_evidence(accounts):
    from connections.account_policy import AccountAdmissionError, require_launch, select_account
    db, legacy, second, third = accounts
    select_account("destination", "codex", account_id=second["id"], db=db)
    with pytest.raises(AccountAdmissionError) as error:
        require_launch("codex", thread_id="destination", account_id=second["id"], db=db)
    assert error.value.reason == "account_runtime_pending"
    assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='destination'").fetchone()[0] == second["id"]
    with pytest.raises(AccountAdmissionError) as default:
        require_launch("codex", account_id=legacy["id"], db=db)
    assert default.value.reason == "account_runtime_pending"  # the Default has no launch exemption either


def test_workspace_rollover_moves_a_limited_projectless_thread_to_the_next_enabled_account(accounts):
    from connections import harness_usage
    from connections.account_policy import select_account, turn_account
    from connections.account_receipts import capture_receipt
    from connections.provider_policy import set_policy
    from vendor_account_fixtures import _ready

    db, legacy, second, _ = accounts
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('workspace','user','codex/test','t','t')")
    db.execute("UPDATE models SET context_window=200000 WHERE id='codex/test'")
    _ready(db, second)
    assert select_account("workspace", "codex", account_id=legacy["id"], db=db) == legacy["id"]
    set_policy(None, "codex", rollover=True, order=[legacy["id"], second["id"]], disabled=[],
               expected_revision=0, db=db)
    harness_usage.record_limited(provider="codex", receipt=capture_receipt("codex", account_id=legacy["id"], db=db),
                                 reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
                                 source="rate_limit_update", db=db)
    harness_usage.record_allowed(provider="codex", receipt=capture_receipt("codex", account_id=second["id"], db=db),
                                 source="rate_limit_update", db=db)

    assert turn_account("codex", thread_id="workspace", model="codex/test", db=db) == (second["id"], None)


def test_project_policy_returns_to_workspace_after_project_override_is_removed(accounts):
    from connections.provider_policy import effective_policy, set_policy, set_use_workspace

    db, legacy, second, _ = accounts
    workspace = set_policy(None, "codex", rollover=True, order=[legacy["id"], second["id"]], disabled=[],
                           expected_revision=0, db=db)
    set_use_workspace("destination-project", use_workspace=False, expected_revision=0, db=db)
    project = set_policy("destination-project", "codex", rollover=False, order=[second["id"]],
                         disabled=[legacy["id"]], expected_revision=workspace["revision"], db=db)
    assert effective_policy("destination-project", "codex", db=db) == {key: value for key, value in project.items() if key != "units"}

    set_use_workspace("destination-project", use_workspace=True, expected_revision=1, db=db)

    assert db.execute("SELECT COUNT(*) FROM project_policy_overrides WHERE project_id='destination-project'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM provider_policies WHERE project_id='destination-project'").fetchone()[0] == 0
    assert effective_policy("destination-project", "codex", db=db) == {key: value for key, value in workspace.items() if key != "units"}


def test_project_override_owns_every_provider_policy(accounts):
    from connections.provider_policy import effective_policy, set_policy

    db, legacy, second, _ = accounts
    db.execute("INSERT INTO project_policy_overrides(project_id,revision) VALUES ('destination-project', 1)")
    db.execute("INSERT INTO provider_policies(project_id,provider_key,rollover,order_json,disabled_json,revision) "
               "VALUES ('destination-project','claude-code',1,NULL,'[]',1)")
    db.commit()
    workspace = set_policy(None, 'codex', rollover=True, order=[second['id'], legacy['id']], disabled=[],
                           expected_revision=0, db=db)

    project = effective_policy('destination-project', 'codex', db=db)

    assert project == {
        'provider_key': 'codex', 'capabilities': workspace['capabilities'], 'scope': 'project',
        'rollover': False, 'order': None, 'disabled': [], 'revision': 0,
    }


def test_first_project_policy_write_clones_all_workspace_provider_rows(accounts):
    from connections.provider_policy import effective_policy, set_policy

    db, _, codex, _ = accounts
    workspace_claude = set_policy(None, 'claude-code', rollover=True, order=None, disabled=[],
                                  expected_revision=0, db=db)
    set_policy('destination-project', 'codex', rollover=True, order=[codex['id']], disabled=[],
               expected_revision=0, db=db)

    project_claude = effective_policy('destination-project', 'claude-code', db=db)

    assert {key: project_claude[key] for key in ('rollover', 'order', 'disabled', 'revision')} == {
        key: workspace_claude[key] for key in ('rollover', 'order', 'disabled', 'revision')
    }
    assert project_claude['scope'] == 'project'


def test_workspace_destination_offer_uses_limited_workspace_threads(accounts, monkeypatch):
    from connections import account_rollover
    from connections.provider_policy import set_policy

    db, default, _, _ = accounts
    set_policy(None, 'codex', rollover=True, order=[default['id']], disabled=[], expected_revision=0, db=db)
    calls = []
    monkeypatch.setattr('infra.connection_limit_release.limited_workspace_threads',
                        lambda provider, *, db: calls.append(provider) or [])
    monkeypatch.setattr('infra.connection_limit_release.limited_project_threads', lambda *_args, **_kwargs: [])

    account_rollover.offer_destination('codex', default['id'], db=db)

    assert calls == ['codex']


def test_policy_payload_orders_units_by_saved_policy_order(accounts):
    from connections.provider_policy import policy_payload, set_policy

    db, first, second, third = accounts
    set_policy('destination-project', 'codex', rollover=False, order=[second['id'], first['id']],
               disabled=[third['id']], expected_revision=0, db=db)

    codex = next(provider for provider in policy_payload('destination-project', db=db)['providers']
                 if provider['provider_key'] == 'codex')

    assert [(unit['id'], unit['enabled']) for unit in codex['units']] == [
        (second['id'], True), (first['id'], True), (third['id'], False),
    ]


def test_turn_selection_and_settings_read_one_account_order(accounts):
    from connections.account_policy import permitted_accounts
    from connections.provider_policy import policy_payload, set_policy
    from connections.vendor_accounts import set_account_state

    db, default, second, third = accounts
    set_account_state(third["id"], "removed", expected_revision=third["revision"], db=db)
    set_policy("destination-project", "codex", rollover=True, order=[second["id"]], disabled=[], expected_revision=0, db=db)

    selection = [a["id"] for a in permitted_accounts("codex", project_ids=["destination-project"], selection_only=True, db=db)]
    full = [a["id"] for a in permitted_accounts("codex", project_ids=["destination-project"], db=db)]
    units = [u["id"] for u in next(p for p in policy_payload("destination-project", db=db)["providers"]
                                   if p["provider_key"] == "codex")["units"]]

    assert selection == full == units == [second["id"], default["id"]]


def test_preloaded_accounts_keep_policy_and_removed_filter(accounts):
    from connections.account_policy import permitted_accounts
    from connections.provider_policy import set_policy
    from connections.vendor_accounts import list_accounts, set_account_state

    db, default, second, third = accounts
    set_account_state(third['id'], 'removed', expected_revision=third['revision'], db=db)
    set_policy('destination-project', 'codex', rollover=True, order=[], disabled=[second['id']],
               expected_revision=0, db=db)
    rows = list_accounts(provider='codex', include_removed=True, db=db)
    loaded = permitted_accounts('codex', project_ids=['destination-project'], db=db, accounts=rows)
    fetched = permitted_accounts('codex', project_ids=['destination-project'], db=db)
    assert [row['id'] for row in loaded] == [row['id'] for row in fetched] == [default['id']]


def test_unsaved_policy_skips_disabled_default_and_denies_when_all_are_disabled(accounts):
    from connections.account_policy import chosen_account_id, turn_account
    from connections.provider_policy import set_policy

    db, default, second, third = accounts
    set_policy('destination-project', 'codex', rollover=False, order=None, disabled=[default['id']],
               expected_revision=0, db=db)

    assert chosen_account_id('destination', 'codex', db=db) == second['id']

    set_policy('destination-project', 'codex', rollover=False, order=None,
               disabled=[default['id'], second['id'], third['id']], expected_revision=1, db=db)

    assert turn_account('codex', thread_id='destination', db=db) == (default['id'], 'account_project_denied')


def test_workspace_disabled_account_is_refused_while_an_unbound_inheriting_thread_selects_next(accounts):
    from connections.account_policy import AccountAdmissionError, select_account, turn_account
    from connections.provider_policy import set_policy

    db, legacy, second, _ = accounts
    set_policy(None, "codex", rollover=False, order=[second["id"]], disabled=[legacy["id"]],
               expected_revision=0, db=db)

    with pytest.raises(AccountAdmissionError, match="account_project_denied"):
        select_account("destination", "codex", account_id=legacy["id"], db=db)
    assert turn_account("codex", thread_id="destination", db=db) == (second["id"], None)


def test_affected_account_threads_filter_provider_subjects_through_canonical_check(accounts, monkeypatch):
    """Provider-bound candidates reach the canonical selector, which picks the changed account."""
    import connections.account_policy as account_policy
    from connections.account_holds import affected_thread_ids

    db, legacy, second, _ = accounts
    account_policy.select_account("destination", "codex", account_id=legacy["id"], db=db)
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('other','user','codex/test','t','t')")
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES ('other','codex',?)", (second["id"],))
    db.commit()
    checked = []

    def canonical(provider, *, thread_id, project, account_id, model, db, binding):
        checked.append((provider, thread_id, model, binding and binding["account_id"]))
        return (legacy["id"] if thread_id == "destination" else second["id"], None)

    # turn_account is this selector once a caller has read the thread's project (and binding)
    monkeypatch.setattr(account_policy, "_scoped_turn_account", canonical)

    assert affected_thread_ids(legacy["id"], db=db) == ["destination"]
    assert checked == [("codex", "destination", "codex/test", legacy["id"]),
                       ("codex", "other", "codex/test", second["id"])]


def test_affected_account_threads_includes_unbound_queued_default_subject(accounts):
    """Queued unbound input must refresh the account selected for its saved model."""
    from connections.account_holds import affected_thread_ids
    from infra.input_admission import accept_thread_input

    db, default, _, _ = accounts
    accept_thread_input("destination", role="user", content="input", intent="apply_now", message_id="queued", db=db)

    assert affected_thread_ids(default["id"], db=db) == ["destination"]


def test_affected_account_threads_includes_rollover_destination_for_limited_binding(accounts):
    """A limited source binding must refresh its rollover destination account."""
    from connections import harness_usage
    from connections.account_holds import affected_thread_ids
    from connections.account_policy import select_account, turn_account
    from connections.account_receipts import capture_receipt
    from connections.provider_policy import set_policy
    from vendor_account_fixtures import _ready

    db, first, second, _ = accounts
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('workspace','user','codex/test','t','t')")
    db.execute("UPDATE models SET context_window=200000 WHERE id='codex/test'")
    _ready(db, second)
    assert select_account("workspace", "codex", account_id=first["id"], db=db) == first["id"]
    set_policy(None, "codex", rollover=True, order=[first["id"], second["id"]],
               disabled=[], expected_revision=0, db=db)
    harness_usage.record_limited(
        provider="codex", receipt=capture_receipt("codex", account_id=first["id"], db=db),
        reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00", source="rate_limit_update", db=db,
    )
    harness_usage.record_allowed(
        provider="codex", receipt=capture_receipt("codex", account_id=second["id"], db=db),
        source="rate_limit_update", db=db,
    )

    assert turn_account("codex", thread_id="workspace", model="codex/test", db=db) == (second["id"], None)
    assert affected_thread_ids(second["id"], db=db) == ["workspace"]


def test_workspace_policy_write_schedules_a_workspace_following_project_head(accounts, monkeypatch):
    """A workspace change must re-drive held heads in projects that inherit it."""
    from connections import harness_usage
    from connections.account_policy import select_account
    from connections.account_receipts import capture_receipt
    from connections.provider_policy import set_policy
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn
    import infra.connection_limit_drive as limit_drive

    db, legacy, second, _ = accounts
    assert select_account("destination", "codex", account_id=legacy["id"], db=db) == legacy["id"]
    harness_usage.record_limited(
        provider="codex", receipt=capture_receipt("codex", account_id=legacy["id"], db=db),
        reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00", source="rate_limit_update", db=db,
    )
    accept_thread_input("destination", role="user", content="input", intent="apply_now", message_id="input", db=db)
    assert type(start_idle_turn("destination", turn_id="held", boot_epoch="test", started_at="t", db=db)).__name__ == "HeldAdmission"
    scheduled = []
    monkeypatch.setattr(limit_drive, "schedule_limited_heads", scheduled.append)

    set_policy(None, "codex", rollover=True, order=[legacy["id"], second["id"]], disabled=[],
               expected_revision=0, db=db)

    assert scheduled == [["destination"]]


def test_policy_change_refreshes_only_threads_it_can_re_hold(accounts, monkeypatch):
    import connections.account_holds as holds
    from connections.account_policy import select_account
    db, legacy, second, third = accounts
    for thread in ("queued", "idle"):
        db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
                   "VALUES (?,'user','codex/test','destination-project','t','t')", (thread,))
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,input_order,delivery_state) "
               "VALUES ('waiting','queued','user','hello','t',1,'queued')")
    db.commit()
    assert select_account("destination", "codex", db=db) == legacy["id"]
    refreshed = []
    original = holds.refresh_thread_holds
    monkeypatch.setattr(holds, "refresh_thread_holds",
                        lambda thread_id, **kw: (refreshed.append(thread_id), original(thread_id, **kw))[1])
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]],
               default_account_id=None, db=db)
    assert sorted(refreshed) == ["destination", "queued"]
    hold = db.execute("SELECT account_id,reason FROM thread_vendor_account_holds WHERE thread_id='destination'").fetchone()
    assert tuple(hold) == (legacy["id"], "account_project_denied")


@pytest.mark.parametrize("denied", ["actor-project", "destination-project"])
def test_creation_applies_both_scopes_and_refusal_creates_nothing(accounts, denied):
    from connections.account_policy import AccountAdmissionError
    from infra.fresh_threads import create_fresh_thread

    db, legacy, second, third = accounts
    set_legacy_policy(denied, "codex", allowlist=[], default_account_id=None, db=db)
    with pytest.raises(AccountAdmissionError) as error:
        create_fresh_thread(user_id="user", thread_id="child", model="codex/test", project_id="destination-project",
                            account_id=legacy["id"], actor_thread_id="actor", db=db)
    assert error.value.reason == "account_project_denied"
    assert db.execute("SELECT COUNT(*) FROM threads WHERE id='child'").fetchone()[0] == 0
    assert _binding(db, "child") is None


def test_creation_binds_intersecting_explicit_account(accounts):
    from connections.account_policy import set_policy
    from infra.fresh_threads import create_fresh_thread

    db, legacy, second, third = accounts
    set_legacy_policy("actor-project", "codex", allowlist=[second["id"], legacy["id"]],
               default_account_id=None, db=db)
    create_fresh_thread(user_id="user", thread_id="child", model="codex/test", project_id="destination-project",
                        account_id=second["id"], actor_thread_id="actor", db=db)
    assert _binding(db, "child") == second["id"]
    assert db.execute("SELECT model FROM threads WHERE id='child'").fetchone()[0] == "codex/test"


def test_agent_switch_request_refused_by_actor_scope_leaves_no_switch(other_model):
    from connections.account_policy import AccountAdmissionError
    from infra.model_switches import request_switch

    db, legacy, second, third = other_model
    set_legacy_policy("actor-project", "codex", allowlist=[], default_account_id=None, db=db)
    with pytest.raises(AccountAdmissionError):
        request_switch("destination", "codex/other", operation_id="op", requested_at="2026-09-30T12:00:00",
                       actor_kind="agent", actor_id="actor", db=db)
    row = db.execute("SELECT model,switch_operation_id,pending_model FROM threads WHERE id='destination'").fetchone()
    assert tuple(row) == ("codex/test", None, None)
    assert _binding(db) is None


def test_activation_race_commits_model_and_holds_instead_of_failing_settlement(other_model):
    from connections.account_policy import AccountAdmissionError, require_launch, set_policy
    from infra.database import write_transaction
    from infra.model_switches import activate_ready_switch, request_switch

    db, legacy, second, third = other_model
    request_switch("destination", "codex/other", operation_id="op", requested_at="2026-09-30T12:00:00",
                   actor_kind="agent", actor_id="actor", db=db)
    assert _binding(db) == legacy["id"]
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]], default_account_id=second["id"], db=db)
    db.execute("UPDATE threads SET switch_status='ready' WHERE id='destination'")
    db.commit()
    with write_transaction(db, scope="test_activation_race") as connection:
        transition = activate_ready_switch(connection, "destination", committed_at="2026-09-30T12:00:02")
    assert transition is not None
    assert db.execute("SELECT model,switch_operation_id FROM threads WHERE id='destination'").fetchone()[:] == (
        "codex/other", None)
    assert _binding(db) == legacy["id"]
    assert _hold(db) == "account_project_denied"
    with pytest.raises(AccountAdmissionError) as error:
        require_launch("codex", thread_id="destination", db=db)
    assert error.value.reason == "account_project_denied"


def test_rehome_keeps_binding_and_recomputes_hold_in_the_same_write(accounts):
    from api.thread_mutations import _write_project_patch
    from connections.account_policy import select_account
    from infra.database import write_transaction

    db, legacy, second, third = accounts
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    set_legacy_policy("actor-project", "codex", allowlist=[second["id"]], default_account_id=None, db=db)
    with write_transaction(db, scope="test_rehome_in") as connection:
        _write_project_patch(connection, "destination", "actor-project", "destination-project", "t1")
    assert (_binding(db), _hold(db)) == (legacy["id"], "account_project_denied")
    assert db.execute("SELECT model FROM threads WHERE id='destination'").fetchone()[0] == "codex/test"
    with write_transaction(db, scope="test_rehome_back") as connection:
        _write_project_patch(connection, "destination", "destination-project", "actor-project", "t2")
    assert (_binding(db), _hold(db)) == (legacy["id"], None)


@pytest.mark.parametrize("busy", ["running", "waiting_retry", "queued_input", "started_head", "pending_switch"])
def test_idle_rebind_refuses_any_live_work(accounts, busy):
    from connections.account_policy import AccountAdmissionError, select_account
    from connections.account_selection import rebind_idle_account

    db, legacy, second, third = accounts
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    if busy in ("running", "waiting_retry"):
        db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,retry_at) "
                   "VALUES ('turn','destination',0,'codex/test',?,'t','boot',?)",
                   (busy, "t" if busy == "waiting_retry" else None))
    elif busy in ("queued_input", "started_head"):
        from infra.input_admission import accept_thread_input

        accept_thread_input("destination", role="user", content="queued", intent="apply_now",
                            message_id="queued", created_at="2026-09-30T12:00:00", db=db)
        if busy == "started_head":
            db.execute("UPDATE messages SET write_started_at='t' WHERE id='queued'")
    else:
        db.execute("UPDATE threads SET switch_operation_id='op' WHERE id='destination'")
    db.commit()
    epoch = _epoch(db)
    before_binding = _binding(db)
    before_switches = db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0]
    if busy == "queued_input":
        moved = rebind_idle_account("destination", third["id"], expected_runtime_epoch=epoch, user_id="user", db=db)
        assert moved == {"account_id": third["id"], "runtime_context_epoch": epoch + 1, "changed": True}
    else:
        with pytest.raises(AccountAdmissionError) as error:
            rebind_idle_account("destination", third["id"], expected_runtime_epoch=epoch, user_id="user", db=db)
        assert error.value.reason == "account_change_requires_idle"
    expected_account = third["id"] if busy == "queued_input" else legacy["id"]
    expected_epoch = epoch + 1 if busy == "queued_input" else epoch
    assert (_binding(db), _epoch(db)) == (expected_account, expected_epoch)
    expected_switches = before_switches + (busy == "queued_input")
    assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.type')='account_switch'").fetchone()[0] == expected_switches
    if busy != "queued_input":
        assert (_binding(db), _epoch(db)) == (before_binding, epoch)


def test_idle_rebind_invalidates_runtime_once_and_projects_hold(accounts):
    from connections.account_policy import AccountAdmissionError, select_account
    from connections.account_selection import rebind_idle_account

    db, legacy, second, third = accounts
    select_account("destination", "codex", account_id=legacy["id"], db=db)
    epoch = _epoch(db)
    with pytest.raises(AccountAdmissionError) as stale:
        rebind_idle_account("destination", third["id"], expected_runtime_epoch=epoch + 1, user_id="user", db=db)
    assert stale.value.reason == "account_runtime_revision_changed"
    with pytest.raises(AccountAdmissionError) as foreign:
        rebind_idle_account("destination", third["id"], expected_runtime_epoch=epoch, user_id="other", db=db)
    assert foreign.value.reason == "account_thread_missing"
    result = rebind_idle_account("destination", third["id"], expected_runtime_epoch=epoch, user_id="user", db=db)
    assert result == {"account_id": third["id"], "runtime_context_epoch": epoch + 1, "changed": True}
    assert (_binding(db), _epoch(db), _hold(db)) == (third["id"], epoch + 1, "account_model_not_discovered")
    assert db.execute("SELECT model FROM threads WHERE id='destination'").fetchone()[0] == "codex/test"
    again = rebind_idle_account("destination", third["id"], expected_runtime_epoch=epoch + 1, user_id="user", db=db)
    assert again == {"account_id": third["id"], "runtime_context_epoch": epoch + 1, "changed": False}
    assert _epoch(db) == epoch + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_fresh_launch_refuses_as_our_own_missing_catalog_row(removed, provider):
    from connections.account_policy import AccountAdmissionError, require_launch
    from infra import send_target
    from infra.send_types import DeliveryFailureSide
    from infra.unavailable_model_recovery import resolve_or_hold
    from infra.vendor_turn_binding import model_vendor, turn_account_id

    db, thread, model = removed.db, THREADS[provider], MODELS[provider]
    before = _bindings(db)
    # Send admission and the drain driver share one resolver; neither reaches the
    # batch beyond the thread identity its project policy is read from.
    batch = _ThreadOnly(thread)
    for launch in (lambda: send_target.resolve_target(model), lambda: resolve_or_hold(model, batch)):
        with pytest.raises(send_target.TargetUnavailable) as refused:
            await launch()
        assert refused.value.side is DeliveryFailureSide.OURS
        assert refused.value.detail == f"Model '{model}' is unavailable: {_missing(model)}"
    # A new Turn for this model freezes no account, and the binding is left alone.
    assert model_vendor(model, db=db) is None
    assert turn_account_id(thread, model, db=db) is None
    assert _bindings(db) == before
    # A warm session frozen on the bound account cannot be fed that unattributed Turn.
    db.execute("UPDATE active_turns SET status='completed',ended_at='t' WHERE thread_id=?", (thread,))
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('new-turn',?,1,?,'running','t','test',NULL)", (thread, model))
    db.commit()
    with pytest.raises(AccountAdmissionError) as mismatch:
        require_launch(provider, thread_id=thread, account_id=removed.bound[provider], turn_id="new-turn",
                       frozen=True, db=db)
    assert mismatch.value.reason == "account_turn_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_resume_refuses_before_touching_the_live_process(removed, provider):
    from llm.resolution import ModelUnavailableError

    thread, model = THREADS[provider], MODELS[provider]
    untouched = _Untouched()
    with pytest.raises(ModelUnavailableError) as refused:
        if provider == "claude-code":
            from subagents.cc_resume_transition import _settled_replacement_boundary

            live = SimpleNamespace(corsia_turn=object(), checkpoint_wait_scope=None, catalog_model_id=model,
                                   session=untouched)
            _settled_replacement_boundary(thread, live)
        else:
            from subagents.codex_turn_transitions import resume_same_turn_after_checkpoint_locked

            live = SimpleNamespace(corsia_turn=object(), session=untouched, catalog_model_id=model,
                                   tool_step_ids=untouched)
            await resume_same_turn_after_checkpoint_locked(
                thread, live, untouched, current_user_text="continue", held_lock=asyncio.Lock())
    assert str(refused.value.reason) == _missing(model)
    assert refused.value.model_id == model


@pytest.mark.parametrize("provider", PROVIDERS)
def test_checkpoint_fork_refuses_without_a_resolved_target(removed, provider):
    from infra.idle_checkpoints import idle_checkpoint_exclusion_reason
    from infra.model_switch_types import SwitchRequest
    from llm.resolution import ModelUnavailableError
    from subagents.harness_checkpoint_context import read_external_checkpoint_context
    from thread import idle_checkpoint, model_checkpoint

    db, thread, model = removed.db, THREADS[provider], MODELS[provider]
    # Account admission takes the resolved target's harness, so every route must resolve first.
    # The manual and harness checkpoint context cannot be read.
    with pytest.raises(ModelUnavailableError) as refused:
        read_external_checkpoint_context(thread, model, provider)
    assert str(refused.value.reason) == _missing(model)
    # A switch checkpoint needs its destination resolved; an unresolved source alone is tolerated.
    request = SwitchRequest("operation", thread, model, model, 0, 1, 0, "user", None, "test", "auto", "t", 0)
    with pytest.raises(ModelUnavailableError) as switch:
        model_checkpoint._switch_targets(db, request)
    assert str(switch.value.reason) == _missing(model)
    # The idle fold is excluded by name and builds no target.
    reason = idle_checkpoint_exclusion_reason(db, thread, model)
    assert reason is not None and reason.name == "UNRESOLVABLE"
    assert idle_checkpoint._resolved_target(db, model, thread).reason.name == "TARGET_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_delegated_spawn_refuses_and_creates_no_child(removed, provider, monkeypatch):
    from llm.delegation_model import eligible_delegation_model
    from subagents import cc_runtime
    from tools import delegate_task

    db, model = removed.db, MODELS[provider]
    threads = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    before = _bindings(db)
    spawned = AsyncMock(side_effect=AssertionError("no child may start"))
    with monkeypatch.context() as local:
        local.setattr(delegate_task, "_spawn_claude_code_child", spawned)
        local.setattr(delegate_task.delegation, "_is_cc_model", _Untouched())
        selected, refusal = eligible_delegation_model(model)
        assert selected is None and refusal.startswith(f"model {model!r} is unknown; choose a ready selector: ")
        result = await delegate_task._spawn("actor", {}, "task", {"model": model})
        assert result == f"Error: {refusal}"
        spawned.assert_not_awaited()
    if provider == "claude-code":
        # The Claude Code route resolves again itself and blocks before a working directory or process.
        with monkeypatch.context() as local:
            local.setattr(cc_runtime, "spawn_child", AsyncMock(side_effect=AssertionError("no child may start")),
                          raising=False)
            blocked = await delegate_task._spawn_claude_code_child(
                parent_thread_id="actor", task="task", title=None, model_id=model)
            assert isinstance(blocked, str)
            assert blocked.startswith("Error: Claude Code delegation blocked: ") and model in blocked
    assert db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == threads
    assert _bindings(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_steer_stays_admitted_on_the_turns_frozen_account(removed, provider):
    """Intended: the Turn was claimed while the row existed, so its account still pays for a steer."""
    db, thread = removed.db, THREADS[provider]
    frozen = db.execute("SELECT vendor_account_id FROM active_turns WHERE id=?", (provider + "-turn",)).fetchone()[0]
    assert frozen == removed.bound[provider]
    writes, observed, accepted = [], [], []
    session = _session(provider, thread, provider + "-turn", writes, launched=True)
    await _steer(provider, session, observed, accepted)
    assert len(writes) == 1 and observed == accepted == [True]
    assert (session.vendor_account_id, session.vendor_account_bound) == (frozen, True)
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id=?",
                      (provider + "-turn",)).fetchone()[0] == frozen
    assert _bindings(db) == [("actor", "codex", removed.added), ("claude-destination", "claude-code", removed.bound["claude-code"]),
                             ("destination", "codex", removed.bound["codex"])]


@pytest.mark.asyncio
async def test_steer_account_gate_still_runs_without_the_catalog_row(removed):
    """The session gate is keyed on the harness, so a missing row never opens a refused account."""
    from connections.account_policy import AccountAdmissionError

    writes, observed, accepted = [], [], []
    session = _session("codex", "actor", "added-turn", writes)
    with pytest.raises(AccountAdmissionError) as refused:
        await _steer("codex", session, observed, accepted)
    assert refused.value.reason == "account_runtime_pending"
    assert writes == observed == accepted == []


@pytest.mark.asyncio
async def test_feed_refuses_when_the_threads_account_moved_during_the_turn(claude):
    from connections.account_policy import AccountAdmissionError, select_account
    from connections.account_selection import rebind_idle_account
    from subagents.cc_stream import CCStreamSession
    from subagents.vendor_write_admission import prepare_session_launch

    db = claude.db
    writes, observed, accepted = [], [], []
    session = CCStreamSession(binary="/bin/synthetic", session_id="test", corsia_thread_id=THREAD)
    session._proc = SimpleNamespace(stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()), returncode=None)
    prepare_session_launch(session, "claude-code")  # the launch freezes the account, the Default's included
    await _feed(session, observed, accepted)
    assert len(writes) == 1 and observed == accepted == [True]
    assert (session.vendor_account_id, session.vendor_account_bound) == (claude.first, True)
    frames = list(session.input_frame_log)

    # No supported route moves a binding under a live turn.
    with pytest.raises(AccountAdmissionError) as busy:
        select_account(THREAD, "claude-code", account_id=claude.other, db=db)
    assert busy.value.reason == "account_change_requires_idle"
    with pytest.raises(AccountAdmissionError) as live:
        rebind_idle_account(THREAD, claude.other, expected_runtime_epoch=0, user_id="user", db=db)
    assert live.value.reason == "account_change_requires_idle"
    assert _claude_binding(db) == claude.first
    # Should the row move anyway, the session gate is the last guard before the paid write.
    db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id=? AND provider='claude-code'",
               (claude.other, THREAD))
    db.commit()
    assert _claude_binding(db) == claude.other != claude.first

    with pytest.raises(AccountAdmissionError) as refused:
        await _feed(session, observed, accepted)
    assert refused.value.reason == "account_session_mismatch"
    # Nothing reached the process, and the session still names the account it froze.
    assert len(writes) == 1 and observed == accepted == [True]
    assert list(session.input_frame_log) == frames
    assert (session.vendor_account_id, session.vendor_account_bound) == (claude.first, True)
    assert _claude_binding(db) == claude.other
    assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='claude-turn'").fetchone()[0] == claude.first


@pytest.mark.parametrize("restricted", ["actor-project", "destination-project"])
@pytest.mark.parametrize("writer", ["direct", "checkpoint", "turn"])
def test_frozen_unknown_writer_denied_by_any_nonempty_policy(accounts, restricted, writer):
    from connections.account_policy import AccountAdmissionError, require_launch, set_policy

    db, legacy, second, third = accounts
    set_legacy_policy(restricted, "codex", allowlist=[second["id"]], default_account_id=second["id"], db=db)
    kwargs = {"thread_id": "destination", "actor_thread_id": "actor", "account_id": None,
              "frozen": True, "db": db}
    if writer == "turn":
        db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
                   "VALUES ('unknown','destination',0,'codex/test','running','t','test',NULL)")
        db.commit()
        kwargs["turn_id"] = "unknown"
    provider_writes = []
    with pytest.raises(AccountAdmissionError) as error:
        admitted = require_launch("codex", **kwargs)
        provider_writes.append(admitted)
    assert error.value.reason == "account_project_denied"
    assert provider_writes == []
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts").fetchone()[0] == 0
    if writer == "turn":
        assert db.execute("SELECT vendor_account_id FROM active_turns WHERE id='unknown'").fetchone()[0] is None


def test_unrestricted_frozen_unknown_remains_unknown_and_native_ignores_vendor_policy(accounts):
    from connections.account_policy import AccountAdmissionError, require_launch, set_policy
    from connections.account_selection import bind_model_account

    db, legacy, second, third = accounts
    with pytest.raises(AccountAdmissionError) as unknown:
        require_launch("codex", thread_id="destination", account_id=None, frozen=True, db=db)
    assert unknown.value.reason == "account_not_configured"  # no ambient login to stand in for an unknown account
    set_legacy_policy("destination-project", "codex", allowlist=[second["id"]], default_account_id=second["id"], db=db)
    assert bind_model_account("destination", "local/native", db=db) is None
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts").fetchone()[0] == 0


@pytest.mark.parametrize("model", [None, "", "   "])
def test_model_less_thread_binds_no_account_and_reads_no_catalog(model):
    import sqlite3

    from connections.account_policy import AccountAdmissionError
    from connections.account_selection import bind_model_account
    from infra.vendor_turn_binding import model_vendor

    db = sqlite3.connect(":memory:")  # No models or account tables: any read would raise.
    assert model_vendor(str(model or ""), db=db) is None
    assert bind_model_account("destination", model, db=db) is None
    with pytest.raises(AccountAdmissionError) as error:
        bind_model_account("destination", model, account_id="acct", db=db)
    assert error.value.reason == "account_requires_vendor_model"


def test_detached_reviewer_session_writes_on_its_own_account_after_the_thread_moved(accounts, tmp_path):
    """A lease review runs on the project-policy account while the requester's thread has moved.

    The reviewer is a non-thread consumer: its session carries the requester's
    thread id only for attribution. Re-checking the thread's *chosen* account
    at the paid write gate refuses every application from a moved thread, so a
    session already admitted through the operation owner must keep its account.
    """
    from connections.account_operation import admit_operation, bind_operation_session, prepare_operation_home
    from connections.account_policy import select_account
    from reviewer.reviewer_account import ReviewerOperation
    from subagents.vendor_write_admission import require_session_write

    db, reviewer, moved, _third = accounts
    select_account("destination", "codex", account_id=moved["id"], db=db)
    request = ReviewerOperation(target=None, vendor_account_id=reviewer["id"], thread_id="destination")
    admitted = prepare_operation_home(admit_operation("codex", "codex/test", request), tmp_path / "detached", kind="detached",
                                    model="codex/test")
    session = bind_operation_session(admitted, SimpleNamespace(corsia_thread_id="destination"),
                                     kind="detached", model="codex/test")
    assert require_session_write(session, "codex") == reviewer["id"]
    assert session.vendor_account_id == reviewer["id"]


def test_a_thread_session_without_an_operation_binding_still_refuses_a_moved_account(accounts):
    """The chosen-account gate keeps guarding a real thread continuation."""
    from connections.account_policy import AccountAdmissionError, select_account
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.vendor_write_admission import prepare_session_launch, require_session_write

    db, frozen, moved, _third = accounts
    select_account("destination", "codex", account_id=frozen["id"], db=db)
    session = CodexAppServerSession(binary="/bin/test", task=None, corsia_thread_id="destination")
    prepare_session_launch(session, "codex")
    assert session.vendor_account_id == frozen["id"]
    db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='destination' AND provider='codex'",
               (moved["id"],))
    db.commit()
    with pytest.raises(AccountAdmissionError) as refused:
        require_session_write(session, "codex")
    assert refused.value.reason == "account_session_mismatch"
    assert session.vendor_account_id == frozen["id"]


def test_operation_binding_refuses_a_kind_the_runtime_does_not_isolate():
    """One owner names operation kinds, so isolation and the write-gate exemption cannot drift."""
    from connections.account_operation import bind_operation_session

    with pytest.raises(ValueError):
        bind_operation_session(SimpleNamespace(), SimpleNamespace(), kind="background")
