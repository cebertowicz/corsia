from __future__ import annotations

from pathlib import Path

import pytest


def test_protected_policy_denies_sensitive_paths_but_preserves_safe_ssh_files(
    tmp_path,
) -> None:
    from tools.host_path_policy import ProtectedHostPathPolicy

    home = tmp_path / "home"
    state_root = tmp_path / "state"
    profile = tmp_path / "generated.sb"
    safehouse = tmp_path / "bin" / "safehouse"
    policy = ProtectedHostPathPolicy(
        home=home,
        state_root=state_root,
        generated_profile=profile,
        safehouse_paths=(safehouse,),
    )

    assert policy.denial(home / ".ssh" / "id_ed25519", for_write=False)
    assert policy.denial(home / ".ssh", for_write=False)
    assert policy.denial(home / ".corsia" / "state.db", for_write=False)
    assert (
        policy.denial(
            state_root / "project.json",
            for_write=False,
        )
        is None
    )
    assert policy.ordinary_denial(
        state_root / "project.json",
        for_write=False,
    )
    assert policy.ordinary_denial(
        tmp_path / "workspace" / ".corsia" / "project.json",
        for_write=False,
    ) is None
    assert policy.denial(
        home / ".config" / "safehouse" / "config.toml",
        for_write=False,
    )
    assert policy.denial(Path("/dev/rdisk4"), for_write=False)
    assert policy.denial(Path("/tmp/corsia-restart"), for_write=True)
    assert policy.denial(
        state_root / "runtime" / "corsia-restart.signal",
        for_write=True,
    )
    assert policy.denial(state_root / "logs" / "restart.log", for_write=True)
    assert policy.denial(state_root / "logs" / "self-restart.log", for_write=True)
    assert policy.denial(profile, for_write=True)
    assert policy.denial(safehouse, for_write=True)
    assert policy.denial(
        home / "Library" / "LaunchAgents" / "com.corsia.dev.plist",
        for_write=True,
    )

    assert policy.denial(home / ".ssh" / "config", for_write=False) is None
    assert policy.denial(home / ".ssh" / "config.d" / "work", for_write=False) is None
    assert policy.denial(home / ".ssh" / "known_hosts", for_write=True) is None
    assert policy.denial(home / "notes.txt", for_write=False) is None

    assert policy.denial(home / ".ssh" / "config", for_write=True)
    assert policy.denial(Path("/tmp/corsia-restart"), for_write=False) is None

    protected_denials = (
        policy.denial(home / ".ssh" / "id_ed25519", for_write=False),
        policy.denial(home / ".corsia" / "state.db", for_write=False),
        policy.denial(Path("/dev/rdisk4"), for_write=False),
        policy.denial(profile, for_write=True),
    )
    assert all("protected mode" in denial for denial in protected_denials if denial)
    assert all(
        "never lease-authorized" not in denial for denial in protected_denials if denial
    )


def test_protected_policy_exposes_only_protected_mode_rules(
    tmp_path,
) -> None:
    from tools.host_path_policy import ProtectedHostPathPolicy

    home = tmp_path / "home"
    state_root = tmp_path / "state"
    profile = tmp_path / "generated.sb"
    safehouse = tmp_path / "safehouse"
    policy = ProtectedHostPathPolicy(
        home=home,
        state_root=state_root,
        generated_profile=profile,
        safehouse_paths=(safehouse,),
    )

    from tools import host_path_policy

    assert not hasattr(host_path_policy, "universal_host_path_denial")
    assert not hasattr(host_path_policy, "UniversalHostPathPolicy")

    protected = policy.sandbox_rules()

    for path in (
        home / ".corsia",
        home / ".config" / "safehouse",
        home / ".ssh",
        profile,
        safehouse,
        Path("/tmp/corsia-restart"),
        state_root / "runtime" / "corsia-restart.signal",
        state_root / "logs" / "restart.log",
        state_root / "logs" / "self-restart.log",
    ):
        assert str(path) in protected
    assert str(state_root) in protected
    assert str(tmp_path / "workspace" / ".corsia") not in protected
    assert str(home / ".ssh" / "config") in protected
    assert str(home / ".ssh" / "known_hosts") in protected


def test_ordinary_policy_keeps_lexical_and_resolved_state_root_boundaries(
    tmp_path,
) -> None:
    from tools.host_path_policy import ProtectedHostPathPolicy

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    public = workspace / "public"
    state.mkdir(parents=True)
    public.mkdir(parents=True)
    state_to_public = state / "public-link"
    public_to_state = public / "state-link"
    composed_alias = workspace / "composed-alias"
    state_alias = workspace / "state-alias"
    loop_a = workspace / "loop-a"
    loop_b = workspace / "loop-b"
    state_to_public.symlink_to(public, target_is_directory=True)
    public_to_state.symlink_to(state, target_is_directory=True)
    composed_alias.symlink_to(state_to_public, target_is_directory=True)
    state_alias.symlink_to(state, target_is_directory=True)
    loop_a.symlink_to(loop_b, target_is_directory=True)
    loop_b.symlink_to(loop_a, target_is_directory=True)
    policy = ProtectedHostPathPolicy(
        home=home,
        state_root=state,
    )

    # Ordinary protection binds both what the agent named and where it resolves.
    assert policy.ordinary_denial(
        state_to_public / "notes.txt",
        for_write=False,
    )
    assert policy.ordinary_denial(
        public_to_state / "state.db",
        for_write=False,
    )
    assert policy.ordinary_denial(
        composed_alias / "notes.txt",
        for_write=False,
    )
    assert policy.ordinary_denial(
        state_alias / ".." / "public" / "notes.txt",
        for_write=False,
    )
    assert policy.ordinary_denial(loop_a / "notes.txt", for_write=False)


def test_live_policy_uses_configured_state_root(monkeypatch, tmp_path) -> None:
    from infra import config
    from tools import host_path_policy

    state_root = tmp_path / "application-state"
    monkeypatch.setattr(config, "STATE_ROOT", state_root)
    monkeypatch.setattr(host_path_policy.shutil, "which", lambda _name: None)

    policy = host_path_policy.current_protected_host_path_policy()

    assert policy.state_root == state_root.resolve(strict=False)
    assert policy.ordinary_denial(state_root / "corsia-prod.db", for_write=False)
    assert policy.ordinary_denial(
        tmp_path / "workspace" / ".corsia" / "old-state.db",
        for_write=False,
    ) is None


@pytest.mark.asyncio
async def test_sensitive_path_enters_exact_lease_review(
    tmp_path,
    monkeypatch,
) -> None:
    from tools import host_access_gate as gate

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    reviewed = []

    lease = {
        "id": "lease-1",
        "revision": 4,
        "application_hash": "a" * 64,
        "state": "active",
    }

    async def active(_thread_id):
        return lease

    async def review(_lease_id, **kwargs):
        reviewed.append(kwargs)
        return {
            "lease": lease,
            "review": {
                "lease_id": "lease-1",
                "lease_revision": 4,
                "thread_id": "thread-1",
                "tool_name": "read_file",
                "action_hash": kwargs["action_hash"],
                "verdict": "allow",
                "permit_id": "permit-1",
            },
        }

    monkeypatch.setattr(gate, "_resolve_active_lease", active)
    monkeypatch.setattr(gate, "_review_host_action", review)

    authorization = await gate.authorize_host_action(
        thread_id="thread-1",
        project_id=None,
        tool=type("Tool", (), {"_workspace": tmp_path})(),
        tool_name="read_file",
        params={"path": str(home / ".ssh" / "id_ed25519")},
    )

    assert authorization.parameter_overrides == {}
    assert len(reviewed) == 1


def _token_policy(tmp_path):
    """Build a minimal policy with a disposable home and state root."""
    from tools.host_path_policy import ProtectedHostPathPolicy

    return ProtectedHostPathPolicy(
        home=tmp_path / "home",
        state_root=tmp_path / "state",
    )


def test_the_api_token_remains_hard_floor_denied(tmp_path) -> None:
    """The token authenticates mutating and admin API routes, so it stays shut.

    A readable token would let a thread enable automatic lease approval via
    ``PATCH /api/settings`` and restart the instance, self-granting the very
    authority host-access review exists to gate.
    """
    policy = _token_policy(tmp_path)
    token = policy.home / ".corsia" / ".api-token"

    assert policy.denial(token, for_write=False)
    assert policy.ordinary_denial(token, for_write=False)
    assert "No host-access lease can open it." in policy.ordinary_denial(token, for_write=False)
    assert policy.hard_floor_denial(token)
    assert f'(deny file-read* file-write* (subpath "{token}"))' in policy.hard_floor_sandbox_rules()


def test_hard_floor_denials_say_that_no_lease_will_open_them(tmp_path) -> None:
    """An agent must not request a lease that cannot possibly help."""
    from tools.host_path_policy import ProtectedHostPathPolicy

    home = tmp_path / "home"
    policy = ProtectedHostPathPolicy(
        home=home,
        state_root=tmp_path / "state",
        generated_profile=tmp_path / "generated.sb",
        safehouse_paths=(tmp_path / "bin" / "safehouse",),
    )

    for path, for_write in (
        (home / ".corsia" / "config.json", False),
        (home / ".ssh" / "id_ed25519", False),
        (Path("/dev/disk0"), False),
        (tmp_path / "generated.sb", True),
        (tmp_path / "bin" / "safehouse", True),
        (Path("/tmp/corsia-restart"), True),
        (home, True),
    ):
        message = policy.denial(path, for_write=for_write)
        assert message is not None, path
        assert "No host-access lease can open it." in message, path


def test_state_root_denial_names_the_resolved_path(tmp_path) -> None:
    """The lease-eligible denial names where the state root actually is."""
    policy = _token_policy(tmp_path)
    state_root = tmp_path / "state"

    message = policy.ordinary_denial(state_root / "corsia.db", for_write=False)

    assert message is not None
    assert str(state_root) in message
    assert "requires reviewed host access" in message
    assert "No host-access lease can open it." not in message


def test_denied_paths_are_read_back_from_the_os_denial_output() -> None:
    """The OS names the refused path; parsing its output beats parsing commands."""
    from tools.host_path_policy import denied_paths_in

    text = (
        "ls: /Users/x/Library/Application Support/: Operation not permitted\n"
        "cat: /Users/x/.corsia/.api-token: Operation not permitted\n"
        "some unrelated line\n"
        "grep: relative/path: Operation not permitted\n"
    )

    found = denied_paths_in(text)

    assert Path("/Users/x/Library/Application Support") in found
    assert Path("/Users/x/.corsia/.api-token") in found
    # A relative path cannot be classified against an absolute policy.
    assert all(path.is_absolute() for path in found)
    assert len(found) == 2


def test_shell_output_guidance_separates_hard_floor_from_lease_eligible(
    tmp_path,
) -> None:
    """The two denial classes must lead an agent to opposite next steps."""
    policy = _token_policy(tmp_path)
    state_root = tmp_path / "state"

    hard = policy.denial_guidance_for_output(
        f"cat: {policy.home / '.corsia' / 'config.json'}: Operation not permitted"
    )
    assert hard is not None
    assert "No host-access lease can open it." in hard

    leaseable = policy.denial_guidance_for_output(
        f"sqlite3: {state_root / 'corsia.db'}: Operation not permitted"
    )
    assert leaseable is not None
    assert str(state_root) in leaseable
    assert "requires reviewed host access" in leaseable
    assert "No host-access lease can open it." not in leaseable


def test_shell_output_guidance_stays_silent_on_unrelated_failures(
    tmp_path,
) -> None:
    """An ordinary command failure must not be dressed up as a policy denial."""
    policy = _token_policy(tmp_path)

    assert policy.denial_guidance_for_output("bash: frobnicate: command not found") is None
    assert policy.denial_guidance_for_output("") is None
    assert (
        policy.denial_guidance_for_output(
            f"cat: {tmp_path / 'workspace' / 'ordinary.txt'}: Operation not permitted"
        )
        is None
    )


def test_composed_shell_denial_leads_with_the_path_then_the_next_step(
    tmp_path,
    monkeypatch,
) -> None:
    """Compose exactly what shell.py emits, using both real producers.

    Not a full shell run: the sandbox refusal itself cannot be provoked
    in-process. This pins the contract between the two halves of the guidance —
    the resolved path first, then the state-aware next step — so a change to
    either cannot silently drop the path an agent needs.
    """
    from tools.host_access_gate import protected_scope_denial
    from tools import host_path_policy

    state_root = tmp_path / "state"
    monkeypatch.setattr(
        host_path_policy,
        "current_protected_host_path_policy",
        lambda **_: host_path_policy.ProtectedHostPathPolicy(
            home=tmp_path / "home",
            state_root=state_root,
        ),
    )

    combined_output = (
        f"sqlite3: {state_root / 'corsia.db'}: Operation not permitted"
    )
    composed = host_path_policy.denial_guidance_prefix(
        combined_output
    ) + protected_scope_denial(
        "this command was blocked by the protected-mode sandbox",
        lease_active=False,
    )

    assert str(state_root) in composed
    assert "request a host-access lease" in composed
    # The path must arrive before the generic next-step template.
    assert composed.index(str(state_root)) < composed.index("Blocked by protected scope")
    # An ordinary failure contributes no prefix at all.
    assert host_path_policy.denial_guidance_prefix("bash: nope: not found") == ""


def test_key_store_folder_is_hard_floor_inside_the_lease_eligible_state_root(
    tmp_path,
) -> None:
    """Stored key values never get the lease-eligible message."""
    policy = _token_policy(tmp_path)
    state_root = tmp_path / "state"
    values = state_root / "keys" / "values.json"
    no_lease = "No host-access lease can open it."

    for for_write in (False, True):
        for path in (values, state_root / "keys", state_root / "keys" / "new.json"):
            for check in (policy.denial, policy.ordinary_denial):
                message = check(path, for_write=for_write)
                assert message is not None, (path, for_write)
                assert no_lease in message, (path, for_write)
                assert "requires reviewed host access" not in message

    # Only the keys folder moved to the hard floor.
    for sibling in (state_root / "corsia.db", state_root / "keys-backup" / "a"):
        message = policy.ordinary_denial(sibling, for_write=False)
        assert message is not None
        assert str(state_root) in message
        assert "requires reviewed host access" in message
        assert no_lease not in message
        assert policy.denial(sibling, for_write=False) is None


def test_key_store_folder_cannot_be_reached_by_symlink_or_dotdot(tmp_path) -> None:
    policy = _token_policy(tmp_path)
    state_root = tmp_path / "state"
    (state_root / "keys").mkdir(parents=True)
    (state_root / "keys" / "values.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "dir-link").symlink_to(state_root / "keys", target_is_directory=True)
    (workspace / "file-link").symlink_to(state_root / "keys" / "values.json")
    (workspace / "state-link").symlink_to(state_root, target_is_directory=True)

    for path in (
        workspace / "dir-link" / "values.json",
        workspace / "file-link",
        workspace / "state-link" / "keys" / "values.json",
        workspace / "state-link" / "other" / ".." / "keys" / "values.json",
    ):
        for for_write in (False, True):
            message = policy.ordinary_denial(path, for_write=for_write)
            assert message is not None, path
            assert "No host-access lease can open it." in message, path


def test_key_store_folder_has_a_sandbox_deny_rule(tmp_path) -> None:
    policy = _token_policy(tmp_path)
    keys = tmp_path / "state" / "keys"

    rules = policy.sandbox_rules()

    assert f'(deny file-read* file-write* (subpath "{keys}"))' in rules
    assert keys in policy.credential_roots
    assert keys not in policy.private_roots


def test_private_roots_stay_protected_mode_only_while_credentials_are_hard_floor(
    tmp_path,
) -> None:
    """A lease may still review ~/.corsia; no lease may open the key store."""
    policy = _token_policy(tmp_path)
    corsia_file = tmp_path / "home" / ".corsia" / "corsia.db"
    key_file = tmp_path / "state" / "keys" / "values.json"

    assert policy.denial(corsia_file, for_write=False)
    assert policy.hard_floor_denial(corsia_file) is None
    assert policy.hard_floor_denial(key_file)
    assert "keys" not in policy.hard_floor_sandbox_rules().replace(
        str(tmp_path / "state" / "keys"), ""
    )
    assert f'(subpath "{corsia_file.parent}")' not in policy.hard_floor_sandbox_rules()


def test_rename_of_an_ancestor_is_pinned_for_every_protected_path_in_a_granted_root(
    tmp_path, monkeypatch,
) -> None:
    from tools import host_path_policy
    from tools.host_path_policy import ProtectedHostPathPolicy
    from tools.sandbox_git import sandbox_literal

    repo = tmp_path / "repo"
    workspace = repo / "ws"
    control = workspace / "ctl" / "restart"
    monkeypatch.setattr(host_path_policy, "_CONTROL_FILES", (control,))
    policy = ProtectedHostPathPolicy(
        home=tmp_path / "home",
        state_root=tmp_path / "state",
        workspace=workspace,
        write_roots=(workspace / "cache" / "apps",),
        granted_roots=(repo,),
    )

    pins = {
        workspace / ".corsia",
        workspace / "ctl",
        workspace / "cache",
        workspace,
        repo,
    }
    assert set(policy.ancestor_pins) == {Path(path).resolve() for path in pins}
    rules = policy.sandbox_rules().splitlines()
    for pin in policy.ancestor_pins:
        assert f'(deny file-write* (literal "{sandbox_literal(pin)}"))' in rules
    assert len(rules) == len(set(rules))
    assert tmp_path.resolve() not in policy.ancestor_pins


def test_ancestors_outside_every_granted_root_are_not_pinned(tmp_path) -> None:
    from tools.host_path_policy import ProtectedHostPathPolicy

    workspace = tmp_path / "ws"
    policy = ProtectedHostPathPolicy(
        home=tmp_path / "home", state_root=tmp_path / "state", workspace=workspace,
    )

    assert set(policy.ancestor_pins) == {(workspace / ".corsia").resolve(), workspace.resolve()}


def test_hard_floor_ignores_path_case(tmp_path):
    from tools.host_path_policy import ProtectedHostPathPolicy

    home = tmp_path / "home"
    state_root = tmp_path / "state"
    (home / ".ssh").mkdir(parents=True)
    (state_root / "keys").mkdir(parents=True)
    policy = ProtectedHostPathPolicy(home=home, state_root=state_root)

    assert policy.ordinary_denial(home / ".SSH" / "ID_ED25519", for_write=False)
    assert policy.hard_floor_denial(state_root / "KEYS" / "provider")
    assert policy.ordinary_denial(home / "notes" / "ssh.txt", for_write=False) is None
