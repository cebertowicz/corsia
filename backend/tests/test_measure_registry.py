"""measure_sync: declaration validation, versioning and the can_decide sync test.

Workflow files here sync for an owner given directly; ``test_measure_ownership`` resolves owners
from real effective skills.
"""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from infra.measure_registry import REGISTRY, WorkflowOwner, sync
from infra.measure_validation import STAMPED_ATTRIBUTES
from infra.outcome_event_attributes import AttributeSpec
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL

FIXTURES = Path(__file__).parent / "fixtures" / "measures"
OWNER = "agent-qg"
NOW_TS = "2026-01-01T12:00:00+00:00"

MEASURES = """
schema: corsia.measures/1
namespace: mail
# agent-level declaration: no workflow
events:
  mail.triaged:
    attributes:
      message_ref: id
      action: {enum: [replied, archived]}
    dedupe: [message_ref, run_id]
kpis:
  replied_share:
    title: Triaged mail answered
    unit: percent
    better: higher
    formula: share
    num: {event: mail.triaged, attr: action, values: [replied]}
    min_n: 20
"""

# A workflow file whose KPIs the tests vary; the event corroborates, so only the named gate fails.
SMALL = """schema: corsia.measures/1
namespace: qa
{header}events:
  qa.done:
    attributes: {{pr: id, verdict: {{enum: [pass, block]}}, cost: num}}
    dedupe: [pr, run_id]
{corroborate}kpis:
{kpis}
"""
GOOD_KPI = "  rate:\n    {body}\n"


def own(agent: str) -> str:
    """The one path an agent-level measures.yaml may sync from."""
    return f"/ws/.threads/{agent}/measures.yaml"


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    REGISTRY.rebuild(connection)
    return connection


def skill_file(tmp_path: Path, text: str) -> str:
    """Write a workflow skill dir with measures.yaml beside its SKILL.md."""
    folder = tmp_path / ".threads" / OWNER / "skills" / "llm-pr-review"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text("---\nname: llm-pr-review\n---\n")
    (folder / "measures.yaml").write_text(text)
    return str(folder / "measures.yaml")


def owned(path: str, owner: str = OWNER) -> dict[str, WorkflowOwner]:
    """The caller's effective workflows: llm-pr-review, in ``path``'s folder, owned by ``owner``."""
    return {"llm-pr-review": WorkflowOwner(Path(path).parent, owner)}


def sync_skill(connection: sqlite3.Connection, path: str, text: str, *, agent: str = OWNER) -> dict:
    return sync(connection, owner_thread_id=agent, path=path, text=text, workflows=owned(path))


def small(kpi_lines: str, *, workflow: bool = True, corroborate: bool = True, extra: str = "") -> str:
    header = ("workflow: llm-pr-review\n" if workflow else "") + extra
    kpis = "".join(f"  {line}\n" if line.strip() else "\n" for line in kpi_lines.strip("\n").splitlines())
    return SMALL.format(header=header, corroborate="    corroborate: plan_node\n" if corroborate else "", kpis=kpis.rstrip("\n"))


def refused(tmp_path: Path, text: str) -> str:
    path = skill_file(tmp_path, text)
    with pytest.raises(ValueError) as caught:
        sync_skill(db(), path, text)
    return str(caught.value)


def deciding(extra: str = "", *, name: str = "rate") -> str:
    return (
        f"{name}:\n  title: Rate\n  unit: percent\n  better: higher\n  formula: share\n"
        f"  num: {{event: qa.done, attr: verdict, values: [pass]}}\n  min_n: 20\n"
        f"  target: 0.9\n  review: 0.8\n  can_decide: true\n{extra}"
    )


def test_sync_then_unchanged_resync_and_changed_version() -> None:
    connection = db()
    first = sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    assert [item["status"] for item in first["items"]] == ["added", "added"]
    again = sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    assert [item["status"] for item in again["items"]] == ["unchanged", "unchanged"]
    changed = sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES.replace("min_n: 20", "min_n: 30"))
    assert [item["status"] for item in changed["items"]] == ["unchanged", "changed v1→v2"]


def test_a_namespace_another_live_agent_holds_is_refused() -> None:
    connection = db()
    connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, lifecycle_state TEXT, archived_reason TEXT)")
    connection.execute("INSERT INTO threads VALUES ('agent-a','active',NULL)")
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    with pytest.raises(ValueError, match=r'Namespace "mail" belongs to another agent \(thread agent-a\); you own no namespace yet'):
        sync(connection, owner_thread_id="agent-b", path=own("agent-b"), text=MEASURES)


def test_event_declarations_stay_in_their_namespace_and_use_a_known_mode(tmp_path: Path) -> None:
    outside_namespace = MEASURES.replace("mail.triaged", "other.triaged")
    assert "event other.triaged is outside namespace mail" in refused(tmp_path, outside_namespace)
    invalid_use = MEASURES.replace("    dedupe: [message_ref, run_id]", "    dedupe: [message_ref, run_id]\n    use: wake")
    assert "event mail.triaged use must be record, route or both" in refused(tmp_path, invalid_use)


@pytest.mark.parametrize("use", ["route", "both"])
def test_a_routed_event_refuses_a_login_and_carries_a_num(use: str) -> None:
    routed = MEASURES.replace("    dedupe: [message_ref, run_id]", f"    dedupe: [message_ref, run_id]\n    use: {use}")
    with_num = routed.replace("      message_ref: id\n", "      message_ref: id\n      cost_usd: num\n")
    sync(db(), owner_thread_id="agent-a", path=own("agent-a"), text=with_num)
    with_login = routed.replace("      message_ref: id\n", "      message_ref: id\n      sender: login\n")
    with pytest.raises(ValueError, match=f'is use: {use}, so attribute sender cannot be "login"'):
        sync(db(), owner_thread_id="agent-a", path=own("agent-a"), text=with_login)


def test_qg_declaration_copy_syncs_with_version_attr_can_decide_and_decides_as(tmp_path: Path) -> None:
    text = (FIXTURES / "qg_measures.yaml").read_text()
    path = skill_file(tmp_path, text)
    connection = db()
    result = sync_skill(connection, path, text)
    statuses = {item["name"]: item["status"] for item in result["items"]}
    assert statuses["qa.review.completed"] == statuses["block_precision"] == statuses["review_cost_per_pr"] == "added"
    assert {item["status"] for item in result["items"]} == {"added"} and len(result["items"]) == 6 + 7 + 11
    REGISTRY.rebuild(connection)  # a restart rebuilds the same declarations from the database
    assert REGISTRY.ensure_loaded(connection).namespaces_of(OWNER) == ["qa"] and "qa.escape.found" in REGISTRY.events
    again = sync_skill(connection, path, text)
    assert {item["status"] for item in again["items"]} == {"unchanged"}


def test_version_attr_needs_an_owned_workflow_with_the_prescribed_text(tmp_path: Path) -> None:
    text = small(deciding(), workflow=False, extra="version_attr: pr\n").replace("can_decide: true\n", "can_decide: false\n")
    message = refused(tmp_path, text)
    line = text.splitlines().index("version_attr: pr") + 1
    assert message == f"measures.yaml line {line}: version_attr is allowed only in a file that names a workflow you own. Add workflow: <name>, or remove version_attr."


def test_version_attr_must_name_a_declared_id_attribute(tmp_path: Path) -> None:
    assert "version_attr nope must name an id attribute declared on an event" in refused(tmp_path, small(deciding(), extra="version_attr: nope\n"))
    assert "version_attr verdict must name an id attribute" in refused(tmp_path, small(deciding(), extra="version_attr: verdict\n"))
    path = skill_file(tmp_path, small(deciding(), extra="version_attr: pr\n"))
    assert sync_skill(db(), path, Path(path).read_text())["namespace"] == "qa"


def test_decides_as_needs_can_decide_and_a_known_value(tmp_path: Path) -> None:
    text = small(deciding().replace("  can_decide: true\n", "") + "  decides_as: spend\n")
    message = refused(tmp_path, text)
    expected_line = text.splitlines().index("    decides_as: spend") + 1
    assert message == f"measures.yaml line {expected_line}: kpi rate decides_as needs can_decide: true. Add can_decide: true, or remove decides_as."
    assert 'kpi rate decides_as "cost" is not one of quality, spend. Use quality or spend.' in refused(tmp_path, small(deciding("  decides_as: cost\n")))


def test_can_decide_must_be_a_boolean(tmp_path: Path) -> None:
    assert "kpi rate can_decide must be true or false" in refused(tmp_path, small(deciding().replace("can_decide: true", "can_decide: yes please")))


def test_can_decide_check_1_needs_target_and_review(tmp_path: Path) -> None:
    no_review = refused(tmp_path, small(deciding().replace("  review: 0.8\n", "")))
    assert "kpi rate can_decide: true needs target and review; review is missing. Add review: <value>, or set can_decide: false." in no_review
    neither = refused(tmp_path, small(deciding().replace("  target: 0.9\n  review: 0.8\n", "")))
    assert "needs target and review; target and review are missing. Add target: <value>, review: <value>, or set can_decide: false." in neither
    zero = small(deciding().replace("target: 0.9", "target: 0").replace("review: 0.8", "review: 0"))
    path = skill_file(tmp_path, zero)
    sync_skill(db(), path, zero)  # a zero target is a value, not a missing field


def test_can_decide_check_2_needs_a_workflow_version(tmp_path: Path) -> None:
    carrying = "kpi rate can_decide: true needs a KPI that can carry a workflow version (no operand anchors on a workflow event). Add match … anchor: true on a workflow event, or set can_decide: false."
    assert carrying in refused(tmp_path, small(deciding(), workflow=False))
    foreign = deciding().replace("event: qa.done, attr: verdict, values: [pass]", "event: github.pr.merged")
    assert carrying in refused(tmp_path, small(foreign))
    anchored = deciding().replace("event: qa.done, attr: verdict, values: [pass]", "event: github.pr.merged, match: [{event: qa.done, on: pr, mode: exists, anchor: true}]")
    text = small(anchored, corroborate=False)  # a platform numerator, versioned through the anchor on a file event
    path = skill_file(tmp_path, text)
    sync_skill(db(), path, text)


def test_can_decide_check_3_needs_an_independent_basis(tmp_path: Path) -> None:
    basis = "kpi rate can_decide: true needs a numerator that a CORSIA emitter writes, that declares corroborate, or a KPI that declares coverage (so another agent's audit can label it). Add coverage: {of: …, by: …}, add corroborate to the event, or set can_decide: false."
    assert basis in refused(tmp_path, small(deciding(), corroborate=False))
    covered = small(deciding("  coverage: {of: {event: qa.done}, by: run_id}\n"), corroborate=False)
    path = skill_file(tmp_path, covered)
    sync_skill(db(), path, covered)  # check 3(c): a coverage line makes it auditable


def test_can_decide_check_4_allows_two_quality_kpis_per_workflow(tmp_path: Path) -> None:
    three = small(deciding(name="first") + deciding(name="second") + deciding(name="third"))
    message = refused(tmp_path, three)
    line = max(i for i, row in enumerate(three.splitlines(), 1) if "can_decide: true" in row)
    assert f"measures.yaml line {line}: kpi third can_decide: true with decides_as quality is the third in workflow llm-pr-review; a workflow may have two. Set can_decide: false on one, or set decides_as: spend if it measures cost." == message
    spend = small(deciding(name="first") + deciding(name="second") + deciding("  decides_as: spend\n", name="third"))
    path = skill_file(tmp_path, spend)
    sync_skill(db(), path, spend)


def test_a_name_left_out_of_the_resynced_file_is_removed_and_no_longer_admitted() -> None:
    from infra.measure_admission import admit

    wider = MEASURES.replace("kpis:", "  mail.sorted:\n    attributes: {message_ref: id}\n    dedupe: [message_ref]\nkpis:")
    connection = db()
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=wider)
    assert admit(connection, owner_thread_id="agent-a", event_name="mail.sorted", attributes={"message_ref": "m1"}).refusal is None
    left_out = sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    assert {item["name"]: item["status"] for item in left_out["items"]} == {"mail.triaged": "unchanged", "replied_share": "unchanged", "mail.sorted": "removed"}
    refused = admit(connection, owner_thread_id="agent-a", event_name="mail.sorted", attributes={"message_ref": "m2"})
    assert refused.refusal and "is not declared in your measures.yaml" in refused.refusal
    assert connection.execute("SELECT count(*) FROM outcome_events WHERE event_name='mail.sorted'").fetchone()[0] == 1  # old rows stay readable
    assert [version for (version,) in connection.execute("SELECT version FROM measure_definitions WHERE name='mail.sorted' ORDER BY version")] == [1, 2]
    again = sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    assert [item["status"] for item in again["items"]] == ["unchanged", "unchanged"]  # a retired name is reported once
    back = sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=wider)
    assert {item["name"]: item["status"] for item in back["items"]}["mail.sorted"] == "added"
    assert admit(connection, owner_thread_id="agent-a", event_name="mail.sorted", attributes={"message_ref": "m3"}).refusal is None


def test_a_name_synced_from_another_file_is_not_removed_by_this_one(tmp_path: Path) -> None:
    connection = db()
    sync(connection, owner_thread_id=OWNER, path=own(OWNER), text=MEASURES)
    other = MEASURES.replace("# agent-level declaration: no workflow", "workflow: llm-pr-review").replace("mail.triaged", "mail.other").replace("replied_share", "other_share")
    result = sync_skill(connection, skill_file(tmp_path, other), other)
    assert all(item["status"] == "added" for item in result["items"])
    assert "mail.triaged" in REGISTRY.events and "mail.other" in REGISTRY.events


def test_the_removal_row_keeps_the_prior_spec_with_removed_set_and_rebuild_skips_it() -> None:
    wider = MEASURES.replace("kpis:", "  mail.sorted:\n    attributes: {message_ref: id}\n    dedupe: [message_ref]\nkpis:")
    connection = db()
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=wider)
    before = connection.execute("SELECT spec_json FROM measure_definitions WHERE name='mail.sorted' AND version=1").fetchone()[0]
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    versions = connection.execute("SELECT version, spec_json, removed FROM measure_definitions WHERE name='mail.sorted' ORDER BY version").fetchall()
    assert versions == [(1, before, 0), (2, before, 1)]
    REGISTRY.rebuild(connection)
    assert "mail.sorted" not in REGISTRY.events and "mail.triaged" in REGISTRY.events


@pytest.mark.parametrize("name", STAMPED_ATTRIBUTES)
def test_one_stamped_set_governs_the_file_the_registry_and_admission(name: str) -> None:
    from infra.measure_admission import admit

    declaring = MEASURES.replace("message_ref: id", f"message_ref: id\n      {name}: id")
    with pytest.raises(ValueError, match=f"attribute {name} is reserved; the server stamps it"):
        sync(db(), owner_thread_id="agent-a", path=own("agent-a"), text=declaring)
    connection = db()
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    assert all(name in event.specs for event in REGISTRY.events.values())  # the registry gives every event a spec for it
    admitted = admit(connection, owner_thread_id="agent-a", event_name="mail.triaged", attributes={"message_ref": "m1", "action": "replied", name: "forged"}, run_id="r")
    assert any(f'attribute "{name}" is stamped by the server' in note for note in admitted.notes)  # admission removes the emitter's value
    assert json.loads(connection.execute("SELECT attributes FROM outcome_events").fetchone()[0]).get(name) != "forged"


def test_kept_attributes_do_not_depend_on_whether_the_registry_is_loaded() -> None:
    from infra import outcome_event_attributes
    from infra.outcome_event_attributes import filter_attributes
    from infra.outcome_events import record_outcome_event

    tree = ast.parse(Path(outcome_event_attributes.__file__).read_text())
    assert "infra.measure_registry" not in {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    specs = {"message_ref": AttributeSpec("id")}
    connection = db()
    for loaded in (False, True):
        REGISTRY.reset()
        if loaded:
            REGISTRY.rebuild(connection)
        assert filter_attributes("mail.triaged", {"message_ref": "m1", "x": "y"}, specs) == ({"message_ref": "m1"}, ["x"])
        assert filter_attributes("mail.triaged", {"message_ref": "m1"}) == ({}, ["message_ref"])  # no specs given: built-in allowlist only
    record_outcome_event(connection, dedupe_key="k", event_name="mail.triaged", source="agent", provenance="agent_recorded", ts=NOW_TS, attributes={"message_ref": "m1"}, declared_specs=specs)
    assert connection.execute("SELECT attributes FROM outcome_events").fetchone()[0] == '{"message_ref":"m1"}'


@pytest.mark.parametrize("value", ["0", "801", "true", "30.5", "'30'"])
def test_keep_days_must_be_a_whole_number_of_days_from_1_to_800(value: str) -> None:
    text = MEASURES.replace("    dedupe: [message_ref, run_id]", f"    dedupe: [message_ref, run_id]\n    keep_days: {value}")
    with pytest.raises(ValueError) as caught:
        sync(db(), owner_thread_id="agent-a", path=own("agent-a"), text=text)
    line = text.splitlines().index(f"    keep_days: {value}") + 1
    assert str(caught.value).startswith(f"measures.yaml line {line}: event mail.triaged keep_days must be a whole number of days from 1 to 800")
    assert str(caught.value).endswith("the default is 400.")


def test_keep_days_defaults_to_400_and_takes_a_value_in_bounds() -> None:
    connection = db()
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    assert REGISTRY.events["mail.triaged"].keep_days == 400
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES.replace("    dedupe: [message_ref, run_id]", "    dedupe: [message_ref, run_id]\n    keep_days: 800"))
    assert REGISTRY.events["mail.triaged"].keep_days == 800


@pytest.mark.parametrize("bad", [
    {"attributes": {"message_ref": "id"}, "dedupe": ["message_ref"], "keep_days": "forever"},
    {"attributes": {"message_ref": "id"}},
    {"dedupe": ["message_ref"]},
    ["not", "a", "mapping"],
])
def test_a_rebuild_skips_one_bad_stored_spec_and_keeps_every_other_event(bad: object) -> None:
    connection = db()
    sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    connection.execute("INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at,removed) VALUES ('agent-b','event','shop.broken',1,?,'/p','sha','t',0)", (json.dumps(bad),))
    REGISTRY.reset()
    REGISTRY.rebuild(connection)
    assert set(REGISTRY.events) == {"mail.triaged"}
