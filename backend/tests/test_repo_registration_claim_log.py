"""Repo registration: the claim and transition log lines, and projects with no folder."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from tests.test_repo_registration_scope import (  # noqa: F401 - shared fixture and helpers
    _held_effect,
    _own_target,
    _register,
    _registered,
    _release,
    env,
)


@contextmanager
def _logged():
    """Loguru INFO lines from the claim and the transition, the ones production log reads capture."""
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(
        lambda message: lines.append(message.record["message"]), level="INFO",
        filter=lambda record: record["name"] in {"api.project_repo_claim", "infra.repo_policy_transition"},
    )
    try:
        yield lines
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
async def test_each_transition_logs_the_claim_it_ran_or_was_refused_under(env):
    target = _own_target(env)
    with _logged() as lines:
        task = await _held_effect(env, "p-agent")
        try:
            assert "Repositories can't change" in await _register(env, target)
        finally:
            await _release(env, task)
        assert "Registered repository 'app'" in await _register(env, target)
    assert lines == [
        "repo policy transition refused claim=pa blocker_thread=p-agent blocker_walled_from=pb",
        "repo policy transition admitted claim=pa",
    ], lines


@pytest.mark.asyncio
async def test_a_folderless_project_does_not_widen_the_claim(env):
    """An archived project with no folder, common on long-lived installs, is no overlap."""
    for pid, name, path in (("pc", "Gone", None), ("pd", "Old", "/nowhere/projects/old")):
        env["db"].execute(
            "INSERT INTO projects (id,name,path,status,created_at,updated_at) VALUES (?,?,?,'archived','t','t')",
            (pid, name, path),
        )
    env["db"].commit()
    target = _own_target(env)
    with _logged() as lines:
        task = await _held_effect(env, "q-agent")
        try:
            result = await _register(env, target)
        finally:
            await _release(env, task)
    assert "Registered repository 'app'" in result, result
    assert _registered(env) == 1
    assert lines == ["repo policy transition admitted claim=pa"], lines


def test_a_folderless_projects_registered_repo_still_counts(env):
    from api.project_repo_claim import registration_claim

    env["db"].execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) VALUES ('pc','Gone',NULL,'archived','t','t')"
    )
    inside = env["alpha"] / "code" / "app"
    inside.mkdir(parents=True)
    env["db"].execute(
        "INSERT INTO project_repo_policies (project_id, repo_name, repo_path, protect_base_checkout) "
        "VALUES ('pc', 'kept', ?, 0)",
        (str(inside),),
    )
    with _logged() as lines:
        claim = registration_claim(
            "pa", env["alpha"], mode="link", path=str(inside), name="app", workspace=env["ws"], db=env["db"],
        )
    assert claim is None
    assert lines == [
        "repo registration claims everywhere project=pa reason=a path overlaps another project's folder or repository"
    ], lines
