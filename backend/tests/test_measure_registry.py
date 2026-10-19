"""measure_sync: declaration validation, workflow ownership and the can_decide sync test."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from infra.measure_registry import REGISTRY, sync
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL

FIXTURES = Path(__file__).parent / "fixtures" / "measures"
OWNER = "agent-qg"

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


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    REGISTRY.rebuild(connection)
    return connection


def skill_file(tmp_path: Path, text: str, *, owner: str | None = OWNER) -> str:
    """Write a workflow skill dir whose SKILL.md names its owner, with measures.yaml beside it."""
    folder = tmp_path / ".threads" / OWNER / "skills" / "llm-pr-review"
    folder.mkdir(parents=True, exist_ok=True)
    frontmatter = f"---\nname: llm-pr-review\ncorsia:\n  workflow:\n    owner: {owner}\n---\n" if owner else "---\nname: llm-pr-review\n---\n"
    (folder / "SKILL.md").write_text(frontmatter)
    (folder / "measures.yaml").write_text(text)
    return str(folder / "measures.yaml")


def small(kpi_lines: str, *, workflow: bool = True, corroborate: bool = True, extra: str = "") -> str:
    header = ("workflow: llm-pr-review\n" if workflow else "") + extra
    kpis = "".join(f"  {line}\n" if line.strip() else "\n" for line in kpi_lines.strip("\n").splitlines())
    return SMALL.format(header=header, corroborate="    corroborate: plan_node\n" if corroborate else "", kpis=kpis.rstrip("\n"))


def refused(tmp_path: Path, text: str) -> str:
    path = skill_file(tmp_path, text)
    with pytest.raises(ValueError) as caught:
        sync(db(), owner_thread_id=OWNER, path=path, text=text)
    return str(caught.value)


def deciding(extra: str = "", *, name: str = "rate") -> str:
    return (
        f"{name}:\n  title: Rate\n  unit: percent\n  better: higher\n  formula: share\n"
        f"  num: {{event: qa.done, attr: verdict, values: [pass]}}\n  min_n: 20\n"
        f"  target: 0.9\n  review: 0.8\n  can_decide: true\n{extra}"
    )


def test_sync_then_unchanged_resync_and_changed_version() -> None:
    connection = db()
    first = sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    assert [item["status"] for item in first["items"]] == ["added", "added"]
    again = sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    assert [item["status"] for item in again["items"]] == ["unchanged", "unchanged"]
    changed = sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES.replace("min_n: 20", "min_n: 30"))
    assert [item["status"] for item in changed["items"]] == ["unchanged", "changed v1→v2"]


def test_namespace_belongs_to_first_syncing_agent() -> None:
    connection = db()
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    with pytest.raises(ValueError, match=r'Namespace "mail" belongs to another agent \(thread agent-a\); you own no namespace yet'):
        sync(connection, owner_thread_id="agent-b", path="/tmp/measures.yaml", text=MEASURES)


def test_workflow_without_skill_owner_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "measures.yaml"
    with pytest.raises(ValueError, match="measure_sync: workflow inbox-triage has no owner, so its measures.yaml can't be synced. Add `corsia: {workflow: {owner: <your thread id>}}` to its SKILL.md frontmatter"):
        sync(db(), owner_thread_id="agent-a", path=str(path), text=MEASURES.replace("# agent-level declaration: no workflow", "workflow: inbox-triage"))
    skill_file(tmp_path, MEASURES, owner=None)
    with pytest.raises(ValueError, match="has no owner"):
        sync(db(), owner_thread_id=OWNER, path=str(tmp_path / ".threads" / OWNER / "skills" / "llm-pr-review" / "measures.yaml"), text=MEASURES.replace("# agent-level declaration: no workflow", "workflow: llm-pr-review"))


def test_workflow_owned_by_another_agent_is_refused(tmp_path: Path) -> None:
    text = small(deciding())
    path = skill_file(tmp_path, text, owner="someone-else")
    with pytest.raises(ValueError, match="workflow llm-pr-review is owned by another agent"):
        sync(db(), owner_thread_id=OWNER, path=path, text=text)


def test_qg_declaration_copy_syncs_with_version_attr_can_decide_and_decides_as(tmp_path: Path) -> None:
    text = (FIXTURES / "qg_measures.yaml").read_text()
    path = skill_file(tmp_path, text)
    connection = db()
    result = sync(connection, owner_thread_id=OWNER, path=path, text=text)
    statuses = {item["name"]: item["status"] for item in result["items"]}
    assert statuses["qa.review.completed"] == statuses["block_precision"] == statuses["review_cost_per_pr"] == "added"
    assert {item["status"] for item in result["items"]} == {"added"} and len(result["items"]) == 6 + 7 + 11
    assert REGISTRY.workflows == {"llm-pr-review": OWNER}
    REGISTRY.rebuild(connection)  # a restart rebuilds the same workflow ownership from the database
    assert REGISTRY.workflows == {"llm-pr-review": OWNER} and "qa.escape.found" in REGISTRY.events
    again = sync(connection, owner_thread_id=OWNER, path=path, text=text)
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
    assert sync(db(), owner_thread_id=OWNER, path=path, text=Path(path).read_text())["namespace"] == "qa"


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
    sync(db(), owner_thread_id=OWNER, path=path, text=zero)  # a zero target is a value, not a missing field


def test_can_decide_check_2_needs_a_workflow_version(tmp_path: Path) -> None:
    carrying = "kpi rate can_decide: true needs a KPI that can carry a workflow version (no operand anchors on a workflow event). Add match … anchor: true on a workflow event, or set can_decide: false."
    assert carrying in refused(tmp_path, small(deciding(), workflow=False))
    foreign = deciding().replace("event: qa.done, attr: verdict, values: [pass]", "event: github.pr.merged")
    assert carrying in refused(tmp_path, small(foreign))
    anchored = deciding().replace("event: qa.done, attr: verdict, values: [pass]", "event: github.pr.merged, match: [{event: qa.done, on: pr, mode: exists, anchor: true}]")
    text = small(anchored, corroborate=False)  # a platform numerator, versioned through the anchor on a file event
    path = skill_file(tmp_path, text)
    sync(db(), owner_thread_id=OWNER, path=path, text=text)


def test_can_decide_check_3_needs_an_independent_basis(tmp_path: Path) -> None:
    basis = "kpi rate can_decide: true needs a numerator that a CORSIA emitter writes, that declares corroborate, or a KPI that declares coverage (so another agent's audit can label it). Add coverage: {of: …, by: …}, add corroborate to the event, or set can_decide: false."
    assert basis in refused(tmp_path, small(deciding(), corroborate=False))
    covered = small(deciding("  coverage: {of: {event: qa.done}, by: run_id}\n"), corroborate=False)
    path = skill_file(tmp_path, covered)
    sync(db(), owner_thread_id=OWNER, path=path, text=covered)  # check 3(c): a coverage line makes it auditable


def test_can_decide_check_4_allows_two_quality_kpis_per_workflow(tmp_path: Path) -> None:
    three = small(deciding(name="first") + deciding(name="second") + deciding(name="third"))
    message = refused(tmp_path, three)
    line = max(i for i, row in enumerate(three.splitlines(), 1) if "can_decide: true" in row)
    assert f"measures.yaml line {line}: kpi third can_decide: true with decides_as quality is the third in workflow llm-pr-review; a workflow may have two. Set can_decide: false on one, or set decides_as: spend if it measures cost." == message
    spend = small(deciding(name="first") + deciding(name="second") + deciding("  decides_as: spend\n", name="third"))
    path = skill_file(tmp_path, spend)
    sync(db(), owner_thread_id=OWNER, path=path, text=spend)


def test_a_name_left_out_of_the_resynced_file_is_removed_and_no_longer_admitted() -> None:
    from infra.measure_admission import admit

    wider = MEASURES.replace("kpis:", "  mail.sorted:\n    attributes: {message_ref: id}\n    dedupe: [message_ref]\nkpis:")
    connection = db()
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=wider)
    assert admit(connection, owner_thread_id="agent-a", event_name="mail.sorted", attributes={"message_ref": "m1"}).refusal is None
    left_out = sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    assert {item["name"]: item["status"] for item in left_out["items"]} == {"mail.triaged": "unchanged", "replied_share": "unchanged", "mail.sorted": "removed"}
    refused = admit(connection, owner_thread_id="agent-a", event_name="mail.sorted", attributes={"message_ref": "m2"})
    assert refused.refusal and "is not declared in your measures.yaml" in refused.refusal
    assert connection.execute("SELECT count(*) FROM outcome_events WHERE event_name='mail.sorted'").fetchone()[0] == 1  # old rows stay readable
    assert [version for (version,) in connection.execute("SELECT version FROM measure_definitions WHERE name='mail.sorted' ORDER BY version")] == [1, 2]
    again = sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    assert [item["status"] for item in again["items"]] == ["unchanged", "unchanged"]  # a retired name is reported once
    back = sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=wider)
    assert {item["name"]: item["status"] for item in back["items"]}["mail.sorted"] == "added"
    assert admit(connection, owner_thread_id="agent-a", event_name="mail.sorted", attributes={"message_ref": "m3"}).refusal is None


def test_a_name_synced_from_another_file_is_not_removed_by_this_one() -> None:
    connection = db()
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    other = MEASURES.replace("mail.triaged", "mail.other").replace("replied_share", "other_share")
    result = sync(connection, owner_thread_id="agent-a", path="/tmp/other/measures.yaml", text=other)
    assert all(item["status"] == "added" for item in result["items"])
    assert "mail.triaged" in REGISTRY.events and "mail.other" in REGISTRY.events


def test_the_removal_row_keeps_the_prior_spec_with_removed_set_and_rebuild_skips_it() -> None:
    wider = MEASURES.replace("kpis:", "  mail.sorted:\n    attributes: {message_ref: id}\n    dedupe: [message_ref]\nkpis:")
    connection = db()
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=wider)
    before = connection.execute("SELECT spec_json FROM measure_definitions WHERE name='mail.sorted' AND version=1").fetchone()[0]
    sync(connection, owner_thread_id="agent-a", path="/tmp/measures.yaml", text=MEASURES)
    versions = connection.execute("SELECT version, spec_json, removed FROM measure_definitions WHERE name='mail.sorted' ORDER BY version").fetchall()
    assert versions == [(1, before, 0), (2, before, 1)]
    REGISTRY.rebuild(connection)
    assert "mail.sorted" not in REGISTRY.events and "mail.triaged" in REGISTRY.events
