"""DCOL-FR5.9 across every write path's document kinds and actors, and its per-write cost.

Each cell writes through the file tools' one write (``managed_write``). Ownership
allows the owner, the owner's temporary subagent and the scope's conductor, and
refuses every other agent. Scope structure refuses independently of ownership: a
thread writes only in its own ``.threads/<id>/`` directory or ``shared/``; the
conductor also writes in the scope root and any thread's directory. A write lands only where both allow it; a refused write
leaves the file unchanged. Only a landed edit by someone other than the owner and
the owner's subagents reaches the owner.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tests.test_owner_only_edit import (
    SUGGEST_TOOL,
    _foreign_dir_doc,
    _lane,
    _managed,
    _own_dir_doc,
    _owner_events,
    _save,
    _scope_root_doc,
    _shared_doc,
    _tool_call,
    authorized_tools as authorized_tools,  # noqa: F401 - the fixture
)
from tests.test_shared_document_ownership import (
    _event_types,
    _shell_write,
    ownership_scope as ownership_scope,  # noqa: F401 - the fixture
)

DOCUMENTS = {
    "shared": _shared_doc,
    "owner_dir": _own_dir_doc,
    "foreign_dir": _foreign_dir_doc,
    "scope_root": _scope_root_doc,
}
ACTORS = ("owner", "worker", "conductor", "other", "plain")
# Where each actor's write lands: ownership and scope structure both allow it.
LANDS = {
    "shared": {"owner", "worker", "conductor"},
    "owner_dir": {"owner", "conductor"},  # another thread's directory, the worker's included, is structure
    "foreign_dir": {"conductor"},  # ``other`` may write there by structure, but does not own the document
    "scope_root": {"conductor"},  # scope structure is the conductor's
}
REPORTED = ("conductor",)


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_actor_on_every_document_kind(ownership_scope, document, actor):
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    path, artifact_id = DOCUMENTS[document](scope)
    before = path.read_text(encoding="utf-8")
    if actor in LANDS[document]:
        _managed(scope, path, actor, "changed\n")
        assert path.read_text(encoding="utf-8") == "changed\n"
    else:
        with pytest.raises(DurableWritePolicyError):
            _managed(scope, path, actor, "changed\n")
        assert path.read_text(encoding="utf-8") == before
    reported = "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)
    assert reported is (actor in REPORTED and actor in LANDS[document])
    assert "ownership_bypass_write" not in _event_types(scope["db"], artifact_id)


# The user saves any document, including one in an agent's ``.threads/<id>/`` directory.
@pytest.mark.parametrize("document", DOCUMENTS)
def test_the_user_saves_an_owned_document_and_the_owner_hears(ownership_scope, document):
    from doceditor.document_save import DocumentSave, _write_document

    scope = ownership_scope
    path, artifact_id = DOCUMENTS[document](scope)
    _write_document(DocumentSave(path=str(path), text="by the user\n", project_id="project-alpha"), path, True)
    assert path.read_text(encoding="utf-8") == "by the user\n"
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)


# --- every write path, every actor, one shared document -------------------------------------

@pytest.fixture
def helpers(ownership_scope):
    """Temporary subagents of the conductor and of a non-owner agent."""
    from tests.registry_helpers import make_session

    scope = ownership_scope
    for key, parent in (("conductor_helper", "conductor"), ("other_helper", "other")):
        scope[key] = make_session(scope["db"], project_id="project-alpha", title=key, prefix=key[:6])
        scope["db"].execute("UPDATE threads SET spawned_by=? WHERE id=?", (scope[parent], scope[key]))
    scope["db"].commit()
    return scope


@pytest.fixture
def owned(helpers):
    """The shared document every cell writes, set up before the test's event loop runs."""
    return _shared_doc(helpers)


AGENTS = ("owner", "worker", "conductor", "conductor_helper", "other", "other_helper")
AGENT_PATHS = ("write_file", "edit_file", "multi_edit", "save", "live", "host_lease")
USER_PATHS = ("save", "live")
MAY_EDIT = {"owner", "worker", "conductor", "conductor_helper", "user"}
TELLS_OWNER = {"conductor", "conductor_helper", "user"}


async def _write_by(scope, tmp_path, write_path, path, actor):
    """Write ``path`` as ``actor`` through one path; return an error text, or None when it landed."""
    from doceditor.live_lane import Operation
    from doceditor.live_publisher import publish_one
    from registry.shared_write_errors import DurableWritePolicyError

    user = actor == "user"
    who = "plain" if user else actor
    try:
        if write_path in ("write_file", "edit_file", "multi_edit"):
            result = await _tool_call(scope, write_path, path, who)
            return result if result.startswith("Error:") else None
        # Blocking writers run off the event loop, as their transports run them.
        if write_path == "host_lease":
            await asyncio.to_thread(_managed, scope, path, who, "ALPHA beta gamma\n", reviewed=True)
        elif write_path == "save":
            await asyncio.to_thread(_save, scope, path, who, direct_user=user)
        else:
            live, lane, write_actor = await _lane(scope, tmp_path, path, who, direct_user=user)
            (await lane.submit(Operation("replace", content="alpha BETA gamma\n", actor=write_actor))).require_landed()
            # The publisher owns the owner report; drain it off the event loop, as it runs.
            while (row := live.store.next_publication()) is not None:
                await asyncio.to_thread(publish_one, live.store, row, db=scope["db"])
    except DurableWritePolicyError as refused:
        return str(refused)
    except Exception as refused:  # the save adapter's own refusal type
        if getattr(refused, "kind", None) != "durable_write_policy":
            raise
        return str(refused)
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", (*AGENTS, "user"))
@pytest.mark.parametrize("write_path", AGENT_PATHS)
async def test_every_write_path_decides_every_actor_the_same_way(
    helpers, owned, authorized_tools, tmp_path, write_path, actor,
):
    if actor == "user" and write_path not in USER_PATHS:
        pytest.skip("the user writes through the editor: save and the live lane")
    scope = helpers
    path, artifact_id = owned
    refusal = await _write_by(scope, tmp_path, write_path, path, actor)
    events = _owner_events(scope["db"], artifact_id)
    if actor not in MAY_EDIT:
        assert refusal is not None and SUGGEST_TOOL in refusal, refusal
        assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
        assert events == []
        return
    assert refusal is None, refusal
    assert path.read_text(encoding="utf-8") != "alpha beta gamma\n"
    if actor in TELLS_OWNER:
        assert [kind for kind, _ in events] == ["content_edited_by_non_owner"], events
        assert events[0][1]["path"] == "shared/documents/owned.md"
        assert set(events[0][1]) == {"actor", "thread_id", "classification", "diff", "path", "source"}
    else:
        assert events == []


@pytest.mark.parametrize("actor", AGENTS)
def test_a_shell_edit_is_decided_like_every_other_write(helpers, actor):
    """Shell cannot be stopped before it lands: an allowed edit is reported, a refused one is a bypass."""
    from tools.shared_write_detection import detect_shell_shared_writes

    scope = helpers
    path, artifact_id = _shared_doc(scope)
    started = time.time_ns()
    _shell_write(path, "shell edit\n", started)
    notices = detect_shell_shared_writes(
        scope[actor], "project-alpha", "printf x > shared/documents/owned.md", started, scope["root"],
    )
    kinds = [kind for kind, _ in _owner_events(scope["db"], artifact_id)]
    if actor not in MAY_EDIT:
        assert kinds == ["ownership_bypass_write"]
        assert len(notices) == 1 and SUGGEST_TOOL in notices[0]
    elif actor in TELLS_OWNER:
        assert kinds == ["content_edited_by_non_owner"] and notices == []
    else:
        assert kinds == [] and notices == []


# --- per-write cost ----------------------------------------------------------------------------

def _statements(scope, write) -> list[str]:
    seen: list[str] = []
    scope["db"].set_trace_callback(seen.append)
    try:
        write()
    finally:
        scope["db"].set_trace_callback(None)
    return seen


def _decision_statements(scope, path, actor) -> list[str]:
    from registry.document_ownership import document_write_authority

    return _statements(scope, lambda: document_write_authority(
        path, workspace=scope["workspace"], thread_id=scope[actor], direct_user=False, db=scope["db"],
    ))


# The decision for one plain write: the file identity and owner, the scope's
# conductor and at most two lineage reads. Measured, not guessed; a change that
# adds a lookup per write must raise this deliberately.
DECISION_BUDGET = 8


@pytest.mark.parametrize("actor", ("owner", "conductor", "worker"))
def test_one_plain_write_decides_ownership_inside_a_fixed_statement_budget(ownership_scope, actor):
    scope = ownership_scope
    path, _ = _scope_root_doc(scope)
    statements = _decision_statements(scope, path, actor)
    assert len(statements) <= DECISION_BUDGET, statements


def test_the_decision_cost_grows_by_one_read_per_spawn_hop(ownership_scope):
    """The one lineage walk reads one thread row per hop; nothing else repeats per hop."""
    from tests.registry_helpers import make_session

    scope = ownership_scope
    path, _ = _scope_root_doc(scope)
    shallow = len(_decision_statements(scope, path, "worker"))
    parent = scope["worker"]
    for depth in range(4):
        child = make_session(scope["db"], project_id="project-alpha", title=f"Depth {depth}", prefix="deep")
        scope["db"].execute("UPDATE threads SET spawned_by=? WHERE id=?", (parent, child))
        parent = child
    scope["db"].commit()
    scope["deep"] = parent
    assert len(_decision_statements(scope, path, "deep")) == shallow + 4


def test_a_plain_owner_write_decides_once_and_reports_nothing(ownership_scope):
    scope = ownership_scope
    path, _ = _own_dir_doc(scope)
    statements = _statements(scope, lambda: _managed(scope, path, "owner", "changed\n"))
    owner_lookups = [sql for sql in statements if "relationship_state IN" in sql]
    assert len(owner_lookups) == 1, owner_lookups
    assert not [sql for sql in statements if "artifact_registration_events" in sql and "INSERT" in sql.upper()]
