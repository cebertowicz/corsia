"""A workflow's worker settings: shape checked at install, resolved per profile for the runner."""

import pytest

from core.skill_sidecar import parse_sidecar
from core.skill_workflow_validation import validate_workflow
from core.workflow_worker import InvalidWorker, UnknownProfile, worker_settings
from infra.skill_install import upstream_workflow

SIDECAR = """\
workflow:
  worker:
    effort: high
    max_turns: 200
    time_limit_minutes: 30
    profiles:
      main: {min_context_tokens: 1000000}
  steps:
    - {id: review, check: Reviewed}
"""


def test_a_sidecar_worker_block_installs_and_resolves_per_profile():
    corsia, errors = parse_sidecar(SIDECAR)
    assert errors == []
    assert validate_workflow(corsia) == []

    base = {"effort": "high", "max_turns": 200, "time_limit_minutes": 30, "min_context_tokens": None}
    assert worker_settings(corsia) == base
    assert worker_settings(corsia, "main") == {**base, "min_context_tokens": 1000000}


def test_a_shared_workflow_with_a_worker_block_installs():
    workflow = upstream_workflow("reviewer", {"corsia.yaml": SIDECAR.encode()})
    assert workflow["workflow"]["worker"]["max_turns"] == 200


def test_no_worker_block_leaves_every_setting_to_the_runner():
    unset = dict.fromkeys(("effort", "max_turns", "time_limit_minutes", "min_context_tokens"))
    assert worker_settings({"workflow": {"steps": []}}) == unset
    assert worker_settings(None) == unset


def test_an_unknown_profile_names_the_known_ones():
    corsia, _errors = parse_sidecar(SIDECAR)
    with pytest.raises(UnknownProfile, match="defines no profile 'release'; its profiles: main"):
        worker_settings(corsia, "release")
    with pytest.raises(UnknownProfile, match="its profiles: none"):
        worker_settings({"workflow": {}}, "main")


@pytest.mark.parametrize(("worker", "message"), [
    ("fast", "corsia.workflow.worker must be a mapping"),
    ({"model": "opus"}, "corsia.workflow.worker.model is an unknown key"),
    ({"effort": "High"}, "corsia.workflow.worker.effort must be one lowercase word"),
    ({"max_turns": 0}, "corsia.workflow.worker.max_turns must be a positive whole number"),
    ({"time_limit_minutes": "30"}, "corsia.workflow.worker.time_limit_minutes must be a positive whole number"),
    ({"min_context_tokens": True}, "corsia.workflow.worker.min_context_tokens must be a positive whole number"),
    ({"profiles": ["main"]}, "corsia.workflow.worker.profiles must be a mapping of profile names"),
    ({"profiles": {"main": {"profiles": {}}}}, "corsia.workflow.worker.profiles.main.profiles is an unknown key"),
    ({"profiles": {"main": {"max_turns": -1}}}, "corsia.workflow.worker.profiles.main.max_turns must be a positive"),
])
def test_install_refuses_a_bad_shape_by_name(worker, message):
    errors = validate_workflow({"workflow": {"worker": worker, "steps": [{"id": "one"}]}})
    assert len(errors) == 1 and errors[0].startswith(message), errors


@pytest.mark.parametrize("corsia", [
    {"workflow": {"worker": "fast"}},
    {"workflow": {"worker": {"max_turns": "200"}}},
    {"workflow": {"worker": {"profiles": {"main": "big"}}}},
    {"workflow": "review"},
])
def test_a_block_edited_after_install_is_refused_not_defaulted(corsia):
    with pytest.raises(InvalidWorker):
        worker_settings(corsia, "main")


def test_a_worker_only_workflow_is_checked_too():
    assert validate_workflow({"workflow": {"worker": {"max_turns": 0}}}) == [
        "corsia.workflow.worker.max_turns must be a positive whole number",
    ]
