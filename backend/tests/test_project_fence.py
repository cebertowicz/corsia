"""The caller's fenced project roots: the set, the four Safehouse renders and the refusal copy."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.project_fence import Fence, FencedRoot, _drop_nested
from tools.sandbox_git import sandbox_literal

CORSIA_AGENT = str(uuid.uuid4())
WORKSPACE_AGENT = str(uuid.uuid4())
OTHER_WORKSPACE_THREAD = str(uuid.uuid4())
ALPHA_AGENT = str(uuid.uuid4())
BETA_AGENT = str(uuid.uuid4())
SECRET_CONDUCTOR = str(uuid.uuid4())


def _rule(path: Path) -> str:
    return f'(deny file-read* file-write* (subpath "{sandbox_literal(path)}"))'


def _forms(path: Path) -> set[str]:
    return {_rule(path), _rule(path.resolve())}


@pytest.fixture
def world(isolated_db, monkeypatch):
    """A workspace under the real $TMPDIR (``/var`` is a link to ``/private/var`` on macOS), as production has it."""
    from infra import config
    from infra.database import get_db

    workspace = Path(tempfile.mkdtemp(prefix="fence-"))
    state = workspace / "state"
    state.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state)
    db = get_db()
    for name, conductor in (("alpha", None), ("beta", None), ("secret", SECRET_CONDUCTOR)):
        (workspace / "projects" / name).mkdir(parents=True)
        db.execute(
            "INSERT INTO projects(id,name,conductor_thread_id,created_at,updated_at) VALUES (?,?,?,'t','t')",
            (f"id-{name}", name, conductor),
        )
    (workspace / "projects" / "legacy").mkdir()
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('id-secret',1)")
    for thread, project, agent in (
        (ALPHA_AGENT, "id-alpha", 1), (BETA_AGENT, "id-beta", 1),
        (WORKSPACE_AGENT, None, 1), (CORSIA_AGENT, None, 1), (OTHER_WORKSPACE_THREAD, None, 0),
    ):
        db.execute(
            "INSERT INTO threads(id,user_id,model,project_id,agent_enabled,created_at,updated_at) "
            "VALUES (?,'user','m',?,?,'t','t')", (thread, project, agent),
        )
    db.execute(
        "INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id', ?, 't')",
        (json.dumps({"session_id": CORSIA_AGENT}),),
    )
    db.commit()
    yield SimpleNamespace(workspace=workspace, db=db)
    shutil.rmtree(workspace, ignore_errors=True)


def _paths(fence: Fence) -> set[Path]:
    return {root.path for root in fence.roots}


def _policy(thread_id):
    from tools.host_path_policy import current_protected_host_path_policy

    return current_protected_host_path_policy(thread_id=thread_id)


def test_project_agent_is_fenced_from_every_other_folder_and_workspace_agents(world):
    from tools.project_fence import fence_for_thread

    projects = world.workspace / "projects"
    threads = world.workspace / ".threads"
    assert _paths(fence_for_thread(ALPHA_AGENT)) == {
        projects / "beta", projects / "secret", projects / "legacy",
        threads / WORKSPACE_AGENT, threads / CORSIA_AGENT,
    }


def test_workspace_agent_is_fenced_from_all_projects_only(world):
    from tools.project_fence import fence_for_thread

    projects = world.workspace / "projects"
    assert _paths(fence_for_thread(WORKSPACE_AGENT)) == {
        projects / "alpha", projects / "beta", projects / "secret", projects / "legacy",
    }


def test_cloud_corsia_agent_is_fenced_from_local_only_projects_and_local_one_from_nothing(world):
    from tools.project_fence import fence_for_thread

    assert _paths(fence_for_thread(CORSIA_AGENT)) == {world.workspace / "projects" / "secret"}
    world.db.execute("UPDATE threads SET local_only = 1 WHERE id = ?", (CORSIA_AGENT,))
    assert fence_for_thread(CORSIA_AGENT).roots == ()


def test_no_caller_or_unknown_thread_is_unfenced(world):
    from tools.project_fence import fence_for_thread

    assert fence_for_thread(None).roots == ()
    assert fence_for_thread(str(uuid.uuid4())).roots == ()


def test_fence_follows_the_switch_on_the_next_call(world):
    from tools.project_fence import fence_for_thread

    assert world.workspace / "projects" / "beta" not in _paths(fence_for_thread(CORSIA_AGENT))
    world.db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('id-beta',1)")
    assert world.workspace / "projects" / "beta" in _paths(fence_for_thread(CORSIA_AGENT))
    world.db.execute("DELETE FROM project_inference_policies WHERE project_id = 'id-beta'")
    assert world.workspace / "projects" / "beta" not in _paths(fence_for_thread(CORSIA_AGENT))


def test_protected_shell_renders_every_fenced_root_and_not_the_callers_own(world):
    rules = _policy(ALPHA_AGENT).sandbox_rules()
    projects = world.workspace / "projects"
    for fenced in (projects / "beta", projects / "secret", projects / "legacy", world.workspace / ".threads" / WORKSPACE_AGENT):
        assert _forms(fenced) <= set(rules.splitlines())
    assert _rule(projects / "alpha") not in rules
    assert _rule(world.workspace / ".threads" / ALPHA_AGENT) not in rules


def test_a_fence_deny_comes_after_every_allow_in_the_protected_rules(world):
    lines = _policy(ALPHA_AGENT).sandbox_rules().splitlines()
    fence_at = lines.index(_rule(world.workspace / "projects" / "beta"))
    assert not [line for line in lines[fence_at:] if line.startswith("(allow ")]


def test_resolved_form_is_emitted_only_when_it_differs(world):
    rules = _policy(ALPHA_AGENT).sandbox_rules().splitlines()
    beta = world.workspace / "projects" / "beta"
    assert rules.count(_rule(beta)) == 1
    assert rules.count(_rule(beta.resolve())) == 1
    real = world.workspace / "elsewhere"
    real.mkdir()
    (world.workspace / "projects" / "linked").symlink_to(real, target_is_directory=True)
    rules = _policy(ALPHA_AGENT).sandbox_rules()
    assert _rule(world.workspace / "projects" / "linked") in rules and _rule(real.resolve()) in rules


def test_leased_shell_renders_only_local_only_roots(world):
    rules = _policy(ALPHA_AGENT).hard_floor_sandbox_rules()
    assert _forms(world.workspace / "projects" / "secret") <= set(rules.splitlines())
    for ordinary in ("beta", "legacy"):
        assert _rule(world.workspace / "projects" / ordinary) not in rules
    assert _rule(world.workspace / ".threads" / WORKSPACE_AGENT) not in rules


def test_external_agent_child_renders_the_callers_fence(world):
    from tools.host_access_external import external_agent_sandbox_rules

    rules = external_agent_sandbox_rules(world.workspace / "p.sb", [world.workspace / "safehouse"], ALPHA_AGENT)
    assert _forms(world.workspace / "projects" / "beta") <= set(rules.splitlines())
    assert _rule(world.workspace / "projects" / "alpha") not in rules


def test_local_mcp_server_renders_every_local_only_project_and_no_ordinary_one(world):
    from tools.safehouse_launch import mcp_private_state_profile

    profile = mcp_private_state_profile(
        world.workspace / "p.sb", world.workspace / "safehouse", world.workspace / "state", world.workspace / "cache",
    )
    assert _forms(world.workspace / "projects" / "secret") <= set(profile.splitlines())
    for ordinary in ("alpha", "beta", "legacy"):
        assert _rule(world.workspace / "projects" / ordinary) not in profile
    assert ".threads" not in profile


def test_nested_roots_are_dropped():
    outer = FencedRoot(Path("/w/projects/a"), "a")
    inner = FencedRoot(Path("/w/projects/a/deep"), "a")
    twin = FencedRoot(Path("/w/projects/a"), "a")
    assert _drop_nested([inner, outer, twin]) == (outer,)


def test_ordinary_project_refusal_copy(world):
    path = world.workspace / "projects" / "beta" / "notes.md"
    message = _policy(ALPHA_AGENT).denial_guidance_for_paths([path])
    assert message == (
        f"`{path}` is in project `beta`, outside your scope. "
        "Ask the CORSIA agent, or use `request_host_access` for reviewed temporary access."
    )
    assert message.isascii()


def test_local_only_project_refusal_copy(world):
    path = world.workspace / "projects" / "secret" / "notes.md"
    message = _policy(ALPHA_AGENT).denial_guidance_for_paths([path])
    assert message == (
        f"`{path}` is in Local only project `secret`, outside your scope. "
        f"Host access doesn't open it. Ask its conductor (`{SECRET_CONDUCTOR}`)."
    )
    assert message.isascii()


def test_shell_output_denial_names_the_fenced_project(world):
    from tools.host_path_policy import denial_guidance_prefix

    path = world.workspace / "projects" / "beta" / "notes.md"
    prefix = denial_guidance_prefix(f"cat: {path}: Operation not permitted", ALPHA_AGENT)
    assert f"`{path}` is in project `beta`, outside your scope." in prefix
    assert denial_guidance_prefix(f"cat: {path}: Operation not permitted", BETA_AGENT) == ""


def test_private_thread_folder_has_no_project_copy(world):
    path = world.workspace / ".threads" / WORKSPACE_AGENT / "notes.md"
    assert _policy(ALPHA_AGENT).denial_guidance_for_paths([path]) is None


@pytest.mark.integration
def test_safehouse_protected_shell_cannot_read_or_write_a_fenced_project(world, monkeypatch, tmp_path):
    import os
    import subprocess

    from tools import sandbox_config, shell_guard, unix_socket_policy

    required = os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") == "1"
    if os.environ.get("CORSIA_THREAD_ID"):
        if required:
            pytest.fail("CORSIA_REQUIRE_SAFEHOUSE=1 cannot run inside an agent shell")
        pytest.skip("agent shells cannot prove nested Safehouse confinement")
    if shell_guard.shutil.which("safehouse") is None:
        if required:
            pytest.fail("CORSIA_REQUIRE_SAFEHOUSE=1 but Safehouse is unavailable")
        pytest.skip("Agent Safehouse is unavailable")
    home = world.workspace / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [world.workspace])
    monkeypatch.setattr(unix_socket_policy, "WORKSPACE", world.workspace)
    own, fenced = world.workspace / "projects" / "alpha", world.workspace / "projects" / "beta"
    (own / "own.txt").write_text("own-marker", encoding="utf-8")
    (fenced / "other.txt").write_text("fenced-marker", encoding="utf-8")
    profile = tmp_path / "profile.sb"

    def run(command: str) -> subprocess.CompletedProcess[str]:
        profile.write_text(
            shell_guard.execution_envelope_profile(command, own, profile, leased=False, thread_id=ALPHA_AGENT),
            encoding="utf-8",
        )
        return subprocess.run(
            shell_guard.safehouse_argv(command, own, profile, thread_id=ALPHA_AGENT),
            capture_output=True, cwd=own, text=True,
            env=shell_guard.safehouse_subprocess_env(ALPHA_AGENT), timeout=20,
        )

    control = run(f"/bin/cat {own / 'own.txt'} && /usr/bin/touch {own / 'made.txt'}")
    if control.returncode != 0:
        pytest.skip(f"the own-project control could not run under Safehouse: {control.stderr.strip()[:300]}")
    assert control.stdout == "own-marker" and (own / "made.txt").exists()

    read = run(f"/bin/cat {fenced / 'other.txt'}")
    write = run(f"/usr/bin/touch {fenced / 'made.txt'}")
    assert read.returncode != 0 and "fenced-marker" not in read.stdout + read.stderr
    assert "Operation not permitted" in read.stderr
    assert write.returncode != 0 and "Operation not permitted" in write.stderr
    assert not (fenced / "made.txt").exists()


def _git_repo(path: Path, *worktrees: str) -> None:
    import subprocess

    def git(*args: str, cwd: Path = path) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True,
        )

    path.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "init")
    for name in worktrees:
        git("worktree", "add", "-q", "--no-track", "-b", f"b-{name[:8]}", str(path / ".worktrees" / name), "main")


def _register(world, project_id: str, repo: Path) -> None:
    world.db.execute(
        "INSERT INTO project_repo_policies(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
        "VALUES (?,?,?,NULL,0)", (project_id, repo.name, str(repo)),
    )
    world.db.commit()


def test_a_repo_in_two_projects_fences_only_the_other_projects_worktree(world):
    from tools.project_fence import fence_for_thread

    repo = world.workspace / "repos" / "shared"
    alpha_tree, beta_tree = f"{ALPHA_AGENT}-feature", f"{BETA_AGENT}-fix"
    _git_repo(repo, alpha_tree, beta_tree)
    _register(world, "id-alpha", repo)
    _register(world, "id-beta", repo)
    canonical_root = repo.resolve()

    from_alpha = fence_for_thread(ALPHA_AGENT)
    assert canonical_root not in {r.path.resolve() for r in from_alpha.roots}
    assert (canonical_root / ".worktrees" / beta_tree) in {r.path.resolve() for r in from_alpha.roots}
    assert (canonical_root / ".worktrees" / alpha_tree) not in {r.path.resolve() for r in from_alpha.roots}
    rules = _policy(ALPHA_AGENT).sandbox_rules()
    assert _rule(canonical_root) not in rules and _rule(canonical_root / ".git") not in rules
    assert _rule(canonical_root / ".worktrees" / beta_tree) in rules
    assert _rule(canonical_root / ".worktrees" / alpha_tree) not in rules

    from_beta = {r.path.resolve() for r in fence_for_thread(BETA_AGENT).roots}
    assert canonical_root not in from_beta
    assert canonical_root / ".worktrees" / alpha_tree in from_beta
    assert canonical_root / ".worktrees" / beta_tree not in from_beta


def test_a_repo_only_another_project_registers_is_fenced_at_its_canonical_root(world):
    from tools.project_fence import fence_for_thread

    repo = world.workspace / "repos" / "private"
    _git_repo(repo, f"{BETA_AGENT}-fix")
    _register(world, "id-beta", repo)
    from_alpha = fence_for_thread(ALPHA_AGENT)
    assert [r.path for r in from_alpha.roots if r.path.name == "private"] == [repo]
    assert not [r for r in from_alpha.roots if ".worktrees" in r.path.parts]
    assert _forms(repo) <= set(_policy(ALPHA_AGENT).sandbox_rules().splitlines())
    assert not [r for r in fence_for_thread(BETA_AGENT).roots if r.path.name == "private"]


def test_a_shared_repo_is_fenced_whole_from_a_workspace_agent_and_a_repo_inside_a_project_adds_no_rule(world):
    from tools.project_fence import fence_for_thread

    shared = world.workspace / "repos" / "shared"
    _git_repo(shared, f"{BETA_AGENT}-fix")
    inside = world.workspace / "projects" / "beta" / "source" / "app"
    _git_repo(inside)
    for repo in (shared, inside):
        _register(world, "id-alpha", repo)
        _register(world, "id-beta", repo)
    paths = _paths(fence_for_thread(WORKSPACE_AGENT))
    assert shared in paths and inside not in paths
    assert not [p for p in paths if ".worktrees" in p.parts]


async def _register_as_alpha(world, monkeypatch, repo: Path) -> str:
    from tools.project_repo_register import ProjectRepoRegisterTool

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: world.workspace))
    return await ProjectRepoRegisterTool().execute(
        repo_path=str(repo), _thread_id=ALPHA_AGENT, _thread_project_id="id-alpha",
    )


def _alpha_rows(world) -> int:
    return world.db.execute("SELECT COUNT(*) FROM project_repo_policies WHERE project_id = 'id-alpha'").fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "copy"), [
    ("beta", "is in project `beta`, outside your scope."),
    ("secret", "is in Local only project `secret`, outside your scope."),
])
async def test_agent_cannot_register_another_projects_repo(world, monkeypatch, owner, copy):
    repo = world.workspace / "repos" / owner
    _git_repo(repo)
    _register(world, f"id-{owner}", repo)
    result = await _register_as_alpha(world, monkeypatch, repo)
    assert result.startswith("Error: `") and copy in result
    assert _alpha_rows(world) == 0


@pytest.mark.asyncio
async def test_agent_cannot_register_a_repo_inside_another_projects_folder(world, monkeypatch):
    repo = world.workspace / "projects" / "beta" / "source" / "app"
    _git_repo(repo)
    result = await _register_as_alpha(world, monkeypatch, repo)
    assert "is in project `beta`, outside your scope." in result
    assert _alpha_rows(world) == 0


@pytest.mark.asyncio
async def test_registering_a_repo_the_caller_already_shares_is_not_refused_by_the_fence(world, monkeypatch):
    repo = world.workspace / "repos" / "shared"
    _git_repo(repo)
    _register(world, "id-alpha", repo)
    _register(world, "id-beta", repo)
    result = await _register_as_alpha(world, monkeypatch, repo)
    assert "outside your scope" not in result


@pytest.mark.asyncio
async def test_agent_can_register_an_unrelated_repo(world, monkeypatch):
    repo = world.workspace / "repos" / "unrelated"
    _git_repo(repo)
    result = await _register_as_alpha(world, monkeypatch, repo)
    assert result.startswith("Registered repository"), result
    assert _alpha_rows(world) == 1
