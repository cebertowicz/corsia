"""Consented discovery of the user's working folders (#3288, design r5)."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from onboarding import consent
from onboarding import project_discovery as discovery
from onboarding import setup_decisions as setup
from plans import service
from plans.boundary import PlanActor
from tests.test_plan_decisions import _mid, _task, db  # noqa: F401 - shared fixtures

SECRET = "my private prompt about payroll"
NOW = datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc)


# --- fixture homes -----------------------------------------------------------


def _claude_session(root, project_dir, cwd, *, mtime, name="s.jsonl", pad=0):
    folder = root / "projects" / project_dir
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    if pad:
        lines.append(json.dumps({"type": "summary", "summary": "x" * pad}))
    lines += [
        json.dumps({"type": "custom-title", "customTitle": SECRET}),
        json.dumps({
            "type": "user", "cwd": str(cwd),
            "message": {"content": SECRET},
        }),
    ]
    path = folder / name
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def _codex_rollout(root, day: datetime.date, cwd, *, mtime, name="rollout-1.jsonl",
                   instructions=""):
    folder = root / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "session_meta",
        "payload": {"id": "u", "cwd": str(cwd), "instructions": instructions or SECRET},
    }
    path = folder / name
    path.write_text(
        json.dumps(meta) + "\n"
        + json.dumps({"type": "response_item", "payload": {"text": SECRET}}) + "\n"
    )
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def home(tmp_path):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    workspace = tmp_path / "corsia-workspace"
    workspace.mkdir()
    return discovery.Roots(claude=claude, codex=codex), workspace, tmp_path


def _repo(tmp_path, name, *, git=True):
    path = tmp_path / "code" / name
    path.mkdir(parents=True)
    if git:
        (path / ".git").mkdir()
    return path


def _ts(days_ago: float) -> float:
    return (NOW - datetime.timedelta(days=days_ago)).timestamp()


def test_lists_both_tools_with_only_the_five_fields_newest_first(home):
    roots, workspace, tmp = home
    alpha, beta = _repo(tmp, "alpha"), _repo(tmp, "beta", git=False)
    _claude_session(roots.claude, "-code-alpha", alpha, mtime=_ts(3))
    _codex_rollout(roots.codex, datetime.date(2026, 9, 29), beta, mtime=_ts(1))

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert [set(row) for row in rows] == [
        {"path", "name", "last_used", "sources", "is_git"},
    ] * 2
    assert rows[0] == {
        "path": str(beta), "name": "beta", "sources": ["codex"], "is_git": False,
        "last_used": datetime.datetime.fromtimestamp(
            _ts(1), datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    assert rows[1]["path"] == str(alpha)
    assert rows[1]["sources"] == ["claude-code"]
    assert rows[1]["is_git"] is True
    assert SECRET not in json.dumps(rows)


def test_missing_roots_list_nothing(home):
    roots, workspace, _tmp = home
    assert discovery.discover(roots, excluded=[workspace], now=NOW) == []


def test_moved_folders_and_the_workspace_are_left_out(home):
    roots, workspace, tmp = home
    kept = _repo(tmp, "kept")
    _claude_session(roots.claude, "a", kept, mtime=_ts(1))
    _claude_session(roots.claude, "b", tmp / "code" / "moved", mtime=_ts(1))
    _claude_session(roots.claude, "c", workspace, mtime=_ts(1))
    inner = workspace / "projects" / "x"
    inner.mkdir(parents=True)
    _claude_session(roots.claude, "d", inner, mtime=_ts(1))

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert [row["path"] for row in rows] == [str(kept)]


def test_every_excluded_root_and_its_session_folders_are_left_out(home):
    roots, workspace, tmp = home
    install = tmp / ".corsia" / "checkout"
    (install / "sessions" / "setup").mkdir(parents=True)
    kept = _repo(tmp, "kept")
    _claude_session(roots.claude, "a", kept, mtime=_ts(1))
    _claude_session(roots.claude, "b", install, mtime=_ts(1))
    _claude_session(roots.claude, "c", install / "sessions" / "setup", mtime=_ts(1))

    rows = discovery.discover(roots, excluded=[workspace, install], now=NOW)

    assert [row["path"] for row in rows] == [str(kept)]


def test_the_tool_excludes_workspace_state_install_and_bootstrap_dir():
    from infra import config
    from tools.project_discovery import _corsia_roots

    roots = {str(path) for path in _corsia_roots()}

    assert str(config.WORKSPACE) in roots
    assert str(config.STATE_ROOT) in roots
    assert str(config.BOOTSTRAP_CONFIG_PATH.parent) in roots
    assert str(Path(config.__file__).resolve().parents[2]) in roots


def test_a_folder_in_both_tools_is_one_row_at_its_newest_use(home):
    roots, workspace, tmp = home
    both = _repo(tmp, "both")
    _claude_session(roots.claude, "a", both, mtime=_ts(5))
    _codex_rollout(roots.codex, datetime.date(2026, 9, 28), both, mtime=_ts(2))
    _codex_rollout(roots.codex, datetime.date(2026, 9, 27), both, mtime=_ts(3),
                   name="rollout-2.jsonl")

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert len(rows) == 1
    assert rows[0]["sources"] == ["claude-code", "codex"]
    assert rows[0]["last_used"].startswith("2026-09-28")


def test_only_the_newest_transcript_per_claude_project_is_read(home):
    roots, workspace, tmp = home
    old, new = _repo(tmp, "old"), _repo(tmp, "new")
    _claude_session(roots.claude, "p", old, mtime=_ts(9), name="old.jsonl")
    _claude_session(roots.claude, "p", new, mtime=_ts(1), name="new.jsonl")

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert [row["name"] for row in rows] == ["new"]


def test_at_most_fifty_rows(home):
    roots, workspace, tmp = home
    for index in range(60):
        _claude_session(roots.claude, f"p{index}", _repo(tmp, f"r{index}"),
                        mtime=_ts(index))

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert len(rows) == discovery.MAX_RESULTS == 50
    assert rows[0]["name"] == "r0"


def test_codex_rollouts_older_than_the_horizon_are_skipped(home):
    roots, workspace, tmp = home
    recent, stale = _repo(tmp, "recent"), _repo(tmp, "stale")
    _codex_rollout(roots.codex, datetime.date(2026, 9, 1), recent, mtime=_ts(29))
    _codex_rollout(roots.codex, datetime.date(2026, 1, 1), stale, mtime=_ts(272))

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert [row["name"] for row in rows] == ["recent"]


def test_reads_at_most_64_kib_of_each_file(home, monkeypatch):
    roots, workspace, tmp = home
    late = _repo(tmp, "late")
    _claude_session(roots.claude, "p", late, mtime=_ts(1), pad=discovery.HEAD_BYTES)
    reads = []
    real_open = open

    def _counting_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if str(path).endswith(".jsonl"):
            real_read = handle.read
            handle_reads = []

            def _read(size=-1):
                handle_reads.append(size)
                return real_read(size)

            reads.append(handle_reads)
            return _Proxy(handle, _read)
        return handle

    class _Proxy:
        def __init__(self, handle, read):
            self._handle, self.read = handle, read

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

    monkeypatch.setattr("builtins.open", _counting_open)

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert rows == []  # the cwd sits past the cap, so it is never read
    assert reads == [[discovery.HEAD_BYTES]]


def test_a_codex_first_line_longer_than_the_cap_still_yields_its_cwd(home):
    roots, workspace, tmp = home
    big = _repo(tmp, "big")
    _codex_rollout(roots.codex, datetime.date(2026, 9, 29), big, mtime=_ts(1),
                   instructions="y" * (2 * discovery.HEAD_BYTES))

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert [row["path"] for row in rows] == [str(big)]


def test_is_git_is_a_stat_and_no_git_command_runs(home, monkeypatch):
    roots, workspace, tmp = home
    worktree = _repo(tmp, "worktree", git=False)
    (worktree / ".git").write_text("gitdir: elsewhere\n")
    _claude_session(roots.claude, "p", worktree, mtime=_ts(1))

    def _refuse(*_args, **_kwargs):
        raise AssertionError("discovery must not run a process")

    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)

    rows = discovery.discover(roots, excluded=[workspace], now=NOW)

    assert rows[0]["is_git"] is True


def test_default_roots_are_the_users_own_never_the_process_environment(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "managed-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "managed-codex"))

    roots = discovery.Roots.for_user()

    assert roots == discovery.Roots(
        claude=tmp_path / ".claude", codex=tmp_path / ".codex",
    )
    assert discovery.Roots.for_user(
        claude=tmp_path / "mine", codex=tmp_path / "theirs",
    ) == discovery.Roots(claude=tmp_path / "mine", codex=tmp_path / "theirs")


# --- consent -----------------------------------------------------------------


def _actor(thread_id="owner", user_id="user-1"):
    return PlanActor(thread_id, user_id, "project-1")


def _human(user_id="user-1"):
    return PlanActor(None, user_id, None, kind="user")


APPS = ("Claude Code", "Codex")
WORDINGS = setup.consent_wordings(APPS)


def _fields(decision):
    return {key: value for key, value in decision.items() if key != "task_title"}


def _find_projects():
    return _fields(setup.find_projects(APPS))


def _decision(db, *, raise_it=True, base=None, **overrides):
    """Draft (and by default raise) the consent decision in owner's plan."""
    from plans import decisions

    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(plan.id, "Find projects", db=db)
    db.commit()
    fields = {**(base or _find_projects()), **overrides}
    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=_mid(), actor=_actor(),
        provenance="tool.plans.decision", db=db, **fields,
    ).result["decision"]
    if raise_it:
        for step in (decisions.parent_review, decisions.raise_to_user):
            decision = step(
                decision["id"], expected_revision=decision["revision"],
                mutation_id=_mid(), actor=_actor(), provenance="p", db=db,
            ).result["decision"]
    db.commit()
    return decision


def _answer(db, decision, option_id=consent.CONSENT_OPTION_ID, other_text=None):
    from plans import decisions

    answered = decisions.answer(
        decision["id"], option_id=option_id, other_text=other_text,
        expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_human(), provenance="api.plans.decision", db=db,
    ).result["decision"]
    db.commit()
    return answered


def _consume(db, decision_id, actor=None, wordings=WORDINGS):
    with service._write(db):
        discovery.consume_consent(db, decision_id, actor or _actor(), wordings)


def _receipts(db):
    return db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE operation=?",
        (discovery.CONSENT_RECEIPT_OPERATION,),
    ).fetchone()[0]


def test_the_hash_binds_the_full_rendered_wording_of_the_consent():
    """Title, whole context and option A label, as the user read them."""
    find = _find_projects()
    assert WORDINGS == [(find["title"], find["context"], "Find my projects")]
    assert [consent.wording_hash(*wording) for wording in WORDINGS] == [
        "7715101439283cd301c4c765fdbce7c7389a3ea788c820d96f4280cca09654be",
    ]
    assert find["title"] == "Look for the projects you already work on?"
    assert "(the start of each session file)" in find["context"]
    assert consent.CONSENT_OPTION_ID == find["options"][0]["id"] == "A"


def test_no_session_folder_means_no_consent_wording():
    assert setup.consent_wordings(()) == []


def test_a_human_answer_a_allows_exactly_one_listing(db, monkeypatch):
    from plans import public

    decision = _answer(db, _decision(db))
    calls: list[str] = []
    answer_actor = public.decision_answer_actor
    load_receipt = public.load_mutation_receipt

    def observed_answer_actor(*args, **kwargs):
        calls.append("answer_actor")
        return answer_actor(*args, **kwargs)

    def observed_load_receipt(*args, **kwargs):
        calls.append("load_receipt")
        return load_receipt(*args, **kwargs)

    monkeypatch.setattr(public, "decision_answer_actor", observed_answer_actor)
    monkeypatch.setattr(public, "load_mutation_receipt", observed_load_receipt)
    _consume(db, decision["id"])
    assert calls == ["answer_actor", "load_receipt"]

    receipt = db.execute(
        "SELECT operation, result_json FROM plan_mutation_receipts WHERE mutation_id=?",
        (f"project_discovery_consent:{decision['id']}",),
    ).fetchone()
    assert receipt["operation"] == "project_discovery_consent"
    assert json.loads(receipt["result_json"]) == {"consumed": True}
    with pytest.raises(consent.ConsentRefused, match="already used"):
        _consume(db, decision["id"])


def test_a_five_option_consent_decision_is_refused(db):
    from plans.decision_fields import DecisionInputError

    options = [
        *_find_projects()["options"],
        {"id": "C", "label": "Another option", "detail": ""},
        {"id": "D", "label": "Another option", "detail": ""},
        {"id": "E", "label": "Another option", "detail": ""},
    ]

    with pytest.raises(DecisionInputError, match="2-4 options"):
        _decision(db, options=options)


def test_a_different_receipt_actor_reusing_consent_refuses_cleanly(db, monkeypatch):
    from plans import public

    decision = _answer(db, _decision(db))
    _consume(db, decision["id"])
    resolve = public.resolve_decision

    def resolve_as_different_actor(*args, **kwargs):
        plan, body, source, _bound = resolve(*args, **kwargs)
        return plan, body, source, PlanActor("different", "user-1", "project-1")

    monkeypatch.setattr(public, "resolve_decision", resolve_as_different_actor)
    with pytest.raises(consent.ConsentRefused, match="already used"):
        _consume(db, decision["id"])


def test_latest_decision_answer_breaks_equal_timestamps_by_insertion_order(db):
    from plans.onboarding_reads import decision_answer_actor

    decision = _decision(db, raise_it=False)
    for event_id, actor_id in (("answer-first", "first"), ("answer-last", "last")):
        db.execute(
            "INSERT INTO plan_node_events "
            "(id, node_id, actor_id, provenance, event, created_at) "
            "VALUES (?, ?, ?, 'test', 'decision_answered', ?) ",
            (event_id, decision["source_node_id"], actor_id, "2026-10-03T00:00:00+00:00"),
        )

    assert decision_answer_actor(db, node_id=decision["source_node_id"]) == "last"


def test_the_describe_it_option_refuses_and_records_nothing(db):
    decision = _answer(db, _decision(db), option_id="B")

    with pytest.raises(consent.ConsentRefused, match="did not choose"):
        _consume(db, decision["id"])
    assert _receipts(db) == 0


def test_a_consent_raised_before_the_facts_changed_refuses(db):
    """The apps found now differ from the ones the user read about."""
    decision = _answer(db, _decision(db))

    with pytest.raises(consent.ConsentRefused, match="fixed discovery consent wording"):
        _consume(db, decision["id"], wordings=setup.consent_wordings(("Codex",)))
    assert _receipts(db) == 0


def test_another_setup_decision_never_authorizes(db):
    github = _fields(next(d for d in setup.build_decisions(setup.Facts())
                          if d["task_title"] == "Set up GitHub"))
    decision = _answer(db, _decision(db, base=github))

    with pytest.raises(consent.ConsentRefused, match="fixed discovery consent wording"):
        _consume(db, decision["id"])


def test_a_pick_any_shaped_answer_never_allows_a_listing(db):
    """The consent stays pick one: a set answer fails closed, even for the consent option alone."""
    from plans import decisions

    decision = _decision(db, pick="any", recommendation=[consent.CONSENT_OPTION_ID])
    answered = decisions.answer(
        decision["id"], option_ids=[consent.CONSENT_OPTION_ID], expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="api.plans.decision", db=db,
    ).result["decision"]
    db.commit()

    with pytest.raises(consent.ConsentRefused, match="did not choose"):
        _consume(db, answered["id"])


def test_other_text_refuses(db):
    decision = _answer(db, _decision(db), option_id="other",
                       other_text="Look for my projects")

    with pytest.raises(consent.ConsentRefused, match="did not choose"):
        _consume(db, decision["id"])


def test_an_unanswered_decision_refuses(db):
    with pytest.raises(consent.ConsentRefused, match="not answered that decision"):
        _consume(db, _decision(db)["id"])


def test_a_decision_never_raised_refuses(db):
    with pytest.raises(consent.ConsentRefused, match="not been raised"):
        _consume(db, _decision(db, raise_it=False)["id"])


def test_an_invalidated_decision_refuses(db):
    from plans import decisions

    decision = _decision(db)
    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_actor(), provenance="p", db=db,
    )
    db.commit()

    with pytest.raises(consent.ConsentRefused, match="withdrawn"):
        _consume(db, decision["id"])


@pytest.mark.parametrize("field, value", [
    ("title", "Want me to find the projects you've been working on?"),
    ("context", "I'll look at your session files."),
    ("context", _find_projects()["context"].replace("the start", "the first line")),
    ("options", [
        {"id": "A", "label": "Look everywhere", "detail": ""},
        {"id": "B", "label": "Not now", "detail": ""},
    ]),
])
def test_reworded_consent_refuses(db, field, value):
    decision = _answer(db, _decision(db, **{field: value}))

    with pytest.raises(consent.ConsentRefused, match="fixed discovery consent wording"):
        _consume(db, decision["id"])


def test_an_answer_the_user_did_not_record_refuses(db):
    """An answered body with no human answer event (written around the route)."""
    from registry import models

    decision = _decision(db)
    row = db.execute(
        "SELECT metadata FROM artifact_identities WHERE external_id=?",
        (decision["id"],),
    ).fetchone()
    body = json.loads(row["metadata"])["plan_decision"]
    body.update(state="answered", answer={"option_id": consent.CONSENT_OPTION_ID, "text": None})
    models.update_metadata(
        "local", "plan-decision", decision["id"], {"plan_decision": body}, db=db,
    )
    db.commit()

    with pytest.raises(consent.ConsentRefused, match="not answered by the user"):
        _consume(db, decision["id"])


def test_a_decision_outside_the_callers_plan_refuses(db):
    decision = _answer(db, _decision(db))

    with pytest.raises(consent.ConsentRefused, match="not in your plan"):
        _consume(db, decision["id"], actor=_actor("worker"))
    with pytest.raises(consent.ConsentRefused, match="not in your plan"):
        _consume(db, "no-such-decision")


# --- tool contract -------------------------------------------------------------


def test_tool_in_first_run_returns_the_setup_decisions(monkeypatch):
    from onboarding import setup_decisions
    from tools.project_discovery import ProjectDiscoveryListTool

    built = {"setup_decisions": [{"title": "One"}], "setup_task_settlements": []}
    build = AsyncMock(return_value=built)
    monkeypatch.setattr(setup_decisions, "setup_plan", build)
    monkeypatch.setattr(setup_decisions, "has_open_setup_task", lambda _tid: True)
    tool = ProjectDiscoveryListTool()
    payload = json.loads(asyncio.run(_execute(tool, _thread_id="owner")))

    assert {key: payload[key] for key in built} == built
    assert "consent_decision" not in payload
    assert "raise the discovery consent decision first" in payload["error"]
    build.assert_awaited_once_with(self_check_passed=True)

    asyncio.run(_execute(tool, _thread_id="owner", self_check_passed=False))
    build.assert_awaited_with(self_check_passed=False)


async def _execute(tool, **kwargs):
    import tools.delegation as delegation

    original = delegation._current_thread_context
    delegation._current_thread_context = lambda _tid: {
        "user_id": "user-1", "project_id": None,
    }
    try:
        return await tool.execute(**kwargs)
    finally:
        delegation._current_thread_context = original


def test_tool_contract_and_catalog_entry():
    from tools.catalog import TOOL_CATALOG
    from tools.project_discovery import ProjectDiscoveryListTool

    tool = ProjectDiscoveryListTool()
    assert tool.name == "project_discovery_list"
    assert set(tool.parameters["properties"]) == {"consent_decision_id", "self_check_passed"}
    assert "USE THIS when the user asked you to find or set up" in tool.description
    assert "consent" in tool.description
    entry = TOOL_CATALOG["project_discovery_list"]
    assert entry.category == "projects"
    assert entry.summary


def test_tool_after_first_run_returns_the_find_projects_consent(monkeypatch, tmp_path):
    from onboarding import setup_decisions
    from tools.project_discovery import ProjectDiscoveryListTool

    (tmp_path / ".codex" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    build = AsyncMock(return_value={"setup_decisions": []})
    monkeypatch.setattr(setup_decisions, "setup_plan", build)
    monkeypatch.setattr(setup_decisions, "has_open_setup_task", lambda _tid: False)
    payload = json.loads(asyncio.run(_execute(ProjectDiscoveryListTool(), _thread_id="owner")))

    assert set(payload) == {"error", "consent_decision"}
    assert payload["consent_decision"] == _fields(setup.find_projects(("Codex",)))
    build.assert_not_awaited()


def test_tool_after_first_run_without_sessions_offers_no_consent(monkeypatch, tmp_path):
    from onboarding import setup_decisions
    from tools.project_discovery import ProjectDiscoveryListTool

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup_decisions, "has_open_setup_task", lambda _tid: False)
    payload = json.loads(asyncio.run(_execute(ProjectDiscoveryListTool(), _thread_id="owner")))

    assert payload == {"error": discovery.NO_SESSIONS}


def test_session_apps_is_one_stat_per_root_and_opens_nothing(tmp_path, monkeypatch):
    """Before consent only the fixed session folders are stat-ed: no listing, no open."""
    from pathlib import Path

    (tmp_path / "claude" / "projects" / "p").mkdir(parents=True)
    (tmp_path / "claude" / "projects" / "p" / "s.jsonl").write_text(SECRET)
    (tmp_path / "codex").mkdir()
    roots = discovery.Roots.for_user(claude=tmp_path / "claude", codex=tmp_path / "codex")

    def refuse(*_a, **_k):
        raise AssertionError("session_apps must not list or open anything")

    # Scoped so the refusals are gone before tmp_path's teardown removes the tree.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "iterdir", refuse)
        patch.setattr(Path, "glob", refuse)
        patch.setattr(Path, "open", refuse)
        patch.setattr(os, "listdir", refuse)
        patch.setattr(os, "scandir", refuse)
        patch.setattr("builtins.open", refuse)

        assert discovery.session_apps(roots) == ["Claude Code"]
        (tmp_path / "codex" / "sessions").mkdir()
        assert discovery.session_apps(roots) == ["Claude Code", "Codex"]
