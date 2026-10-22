"""Shell command guards and sandbox helpers."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from connections.github_agent import AgentAccountView
from host_access.redaction import (
    CommandRedactionError,
    redact_shell_command,
)
from host_access.refusal_messages import (
    LITERAL_CREDENTIAL_REFUSAL,
    normalization_refusal,
)
from infra.project_repo_policy import RepoPolicy
from tools.host_access_external import external_agent_sandbox_rules
from tools.base_git_paths import base_branch_selector_paths, base_git_control_paths, base_git_identity_paths
from tools.host_path_policy import current_protected_host_path_policy
from tools.repo_worktree_guidance import branch_pinned_worktree_guidance
from tools.shell_argv import command_tokens, direct_argv
from tools.sandbox_config import (
    get_allowed_dirs_ro,
    get_allowed_dirs_rw,
    get_sandbox_add_dirs_rw,
    is_safehouse_available,
    list_registered_repo_policies,
    list_unsupported_pinned_repos,
    list_repo_worktrees,
    registered_repo_for_path,
    repo_worktree_root,
)
from tools.sandbox_git import sandbox_literal
from tools.shell_environment import safehouse_subprocess_env as safehouse_subprocess_env
from tools.unix_socket_policy import unix_socket_sandbox_rules
from tools.shell_environment import agent_account_deny_rule, agent_account_ro_dirs, agent_tool_ro_dirs, shell_env_pass_names

_WORKTREE_FLAGS_WITH_VALUE = {
    "-b",
    "-B",
    "--expire",
    "--reason",
}
_WORKTREE_MUTATION_ACTIONS = {
    "add",
    "lock",
    "move",
    "prune",
    "remove",
    "repair",
    "unlock",
}
_WORKTREE_PATH_ACTIONS = _WORKTREE_MUTATION_ACTIONS - {"prune"}
_WORKTREE_DIRECT_ERROR = (
    "Error: use a direct git worktree command with the tool's working "
    "directory set to the owning repository."
)
AvailabilityCheck = Callable[[], bool]

_MIN_SAFEHOUSE_VERSION = (0, 11, 1)
_NO_CLIPBOARD = '(deny mach-lookup (global-name "com.apple.pasteboard.1"))'  # agent-browser -> chromium-full grants it
_HOME = Path.home()
_HOME_LITERAL_PATTERN = re.escape(
    str(_HOME.expanduser().resolve(strict=False))
)


_PIPE_TO_SHELL_RE = re.compile(
    r"\b(curl|wget|fetch)\b[^|]*\|\s*(bash|sh|zsh|ksh|fish|tcsh|csh)\b",
    re.IGNORECASE,
)
_PROC_SUB_FETCH_RE = re.compile(
    r"(?:^|[\s;&|])(?:bash|sh|zsh|ksh|fish|tcsh|csh|source|\.)\s+<\(\s*(?:curl|wget|fetch)\b",
    re.IGNORECASE,
)
_EVAL_FETCH_RE = re.compile(
    r"\b(eval|exec)\b[^$]*\$\(\s*(curl|wget|fetch)\b",
    re.IGNORECASE,
)
_SAFE_DIRECT_BRANCH_FLAGS = {"--no-pager"}

_RCE_GUIDANCE = (
    "Fetching and executing remote code in one step is blocked. "
    "Download and inspect the artifact first, then execute it as a separate "
    "reviewed step."
)


def check_remote_code_execution(command: str) -> str | None:
    """Return an error when a shell command fetches and executes remote code."""
    if _PIPE_TO_SHELL_RE.search(command):
        return f"Error: piping fetched code to a shell is blocked. {_RCE_GUIDANCE}"
    if _PROC_SUB_FETCH_RE.search(command):
        return f"Error: executing fetched code via process substitution is blocked. {_RCE_GUIDANCE}"
    if _EVAL_FETCH_RE.search(command):
        return f"Error: eval/exec of fetched code is blocked. {_RCE_GUIDANCE}"
    return None


_PROTECTED_COMMAND_DENIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "privilege escalation",
        re.compile(
            r"(?:^|[;&|]\s*)(?:/[A-Za-z0-9_./-]+/)?(?:sudo|doas|su)(?:\s|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive disk or raw-device operation",
        re.compile(
            r"(?:\b(?:mkfs(?:\.\w+)?|newfs(?:_\w+)?|fdisk)\b"
            r"|\bdiskutil\s+(?:erase|partition|zero|secureErase)"
            r"|(?:^|[\s>=])/(?:dev/)?r?disk\d)",
            re.IGNORECASE,
        ),
    ),
    (
        "host power operation",
        re.compile(
            r"(?:^|[;&|]\s*)(?:/[A-Za-z0-9_./-]+/)?"
            r"(?:shutdown|reboot|halt|poweroff)(?:\s|$)"
            r"|\bpmset\s+(?:sleepnow|shutdown|restart)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "fork bomb",
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    ),
    (
        "literal root or home erasure",
        re.compile(
            r"\brm\s+(?:-[A-Za-z]*[rf][A-Za-z]*\s+)+"
            r"(?:--\s+)?(?:"
            r"[\"']?/[\"']?"
            rf"|[\"']?{_HOME_LITERAL_PATTERN}/?[\"']?"
            rf"|[\"']{_HOME_LITERAL_PATTERN}[\"']/"
            r"|~/?"
            r"|[\"']?\$(?:HOME|\{HOME\})/?[\"']?"
            r"|[\"']\$(?:HOME|\{HOME\})[\"']/"
            r")(?=\s|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "CORSIA control-plane operation",
        re.compile(
            r"\bcorsia\s+(?:dev\s+up\b|"
            r"(?:(?:dev|service)\s+)?(?:reload|restart|stop)\b)"
            r"|\b(?:launchctl|systemctl)\b[^\n;&|]*"
            r"\b(?:bootout|kickstart|kill|restart|stop|unload)\b"
            r"[^\n;&|]*\bcorsia\b",
            re.IGNORECASE,
        ),
    ),
)


def check_command_safety(command: str) -> str | None:
    """Apply protected-mode terminal guards before any permit is consumed."""
    if remote_error := check_remote_code_execution(command):
        return remote_error
    for label, pattern in _PROTECTED_COMMAND_DENIES:
        if pattern.search(command):
            return (
                "Error: Command blocked by protected-mode safety guard "
                f"({label}). Request reviewed host access, then retry the "
                "exact action."
            )
    try:
        redacted = redact_shell_command(command)
    except CommandRedactionError as exc:
        return f"Error: {normalization_refusal(exc)}"
    if redacted.literal_credentials:
        return f"Error: {LITERAL_CREDENTIAL_REFUSAL}"
    return None


def check_worktree_targets(command: str, cwd: str) -> str | None:
    """Validate the small direct-Git surface granted for pinned repositories.

    This function parses only literal direct Git commands so it does not grow
    into a shell parser. Safehouse remains the backstop for non-literal forms:
    reciprocally verified worktree inventory grants Git metadata independently
    of cwd while later denies keep shared controls protected.
    """
    direct = _direct_git_command(command, cwd)
    if direct is None:
        return _nonliteral_git_error(command, cwd)

    target, subcommand, args = direct
    worktree_mutation = (
        subcommand == "worktree"
        and bool(args)
        and args[0] in _WORKTREE_MUTATION_ACTIONS
    )
    repo_policy = _registered_repo_for_path(target)
    if (
        repo_policy is not None
        and target == repo_policy.path.resolve(strict=False)
        and subcommand == "worktree"
        and bool(args)
        and args[0] in _WORKTREE_PATH_ACTIONS
    ):
        worktree_paths = _worktree_path_args(args[1:])
        if args[0] == "add":
            worktree_paths = worktree_paths[:1]
        for worktree_path in worktree_paths:
            if error := _validate_worktree_target(
                worktree_path,
                str(repo_policy.path),
            ):
                return error
    if (
        repo_policy is not None
        and repo_policy.protect_base_checkout
        and target == repo_policy.path.resolve(strict=False)
        and subcommand == "worktree"
        and bool(args)
        and args[0] == "add"
        and _worktree_add_creates_branch(args)
    ):
        return (
            "Error: new-branch worktrees for a protected base checkout must use "
            "the `worktree_create` tool, which fetches the configured remote "
            "before creating the branch. Raw worktree creation remains available "
            "for unprotected repositories."
        )

    policy = _registered_repo_for_path(target, pinned_only=True)
    if policy is None:
        return None

    if worktree_mutation and target != policy.path.resolve(strict=False):
        return _WORKTREE_DIRECT_ERROR

    if subcommand == "worktree" and args:
        action = args[0]
        if _worktree_add_creates_tracked_branch(args, target):
            return (
                "Error: linked worktree branch creation from a protected base "
                "must use `--no-track` without `--track`; Git otherwise "
                "rewrites shared "
                "repository config after creating the branch. This command "
                "was rejected before Git made any changes. Retry the direct "
                "command as `git worktree add --no-track -b <new-branch> "
                ".worktrees/<slug> <start-point>`."
            )
        if action in _WORKTREE_PATH_ACTIONS:
            worktree_paths = _worktree_path_args(args[1:])
            if action == "add":
                worktree_paths = worktree_paths[:1]
            for worktree_path in worktree_paths:
                if error := _validate_worktree_target(
                    worktree_path,
                    str(policy.path),
                ):
                    return error

    if repo_worktree_root(target, policy.path) is not None:
        return None

    if (
        subcommand == "pull"
        and controlled_fast_forward_policy(
            command,
            cwd,
            policies=[policy],
        )
        is None
    ):
        return (
            "Error: a branch-pinned base checkout only permits the exact "
            "literal `git pull --ff-only` control operation."
        )
    if _direct_branch_mutation(subcommand, args):
        return f"Error: {branch_pinned_worktree_guidance(policy)}"
    if subcommand == "pull" and args != ["--ff-only"]:
        return (
            "Error: a branch-pinned base checkout only permits "
            "`git pull --ff-only` from its configured upstream."
        )
    if subcommand == "fetch" and not _is_allowed_base_fetch(args, policy.path):
        return (
            "Error: a branch-pinned base checkout only permits `git fetch` "
            "or `git fetch <configured-remote>`."
        )
    return None


def _nonliteral_git_error(command: str, cwd: str) -> str | None:
    """Give direct-command guidance for an obvious ungrantable Git form."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) < 2 or Path(argv[0]).name != "git":
        return None
    if (
        argv[1] == "worktree"
        and len(argv) >= 3
        and argv[2] in _WORKTREE_MUTATION_ACTIONS
    ):
        return _WORKTREE_DIRECT_ERROR
    policy = _registered_repo_for_path(Path(cwd), pinned_only=True)
    if (
        policy is not None
        and repo_worktree_root(Path(cwd), policy.path) is None
        and (
            argv[1] == "pull"
            or argv[1] in {"checkout", "switch"}
            or (argv[1] == "branch" and _branch_mutates(argv[2:]))
            or (
                argv[1] == "symbolic-ref"
                and _symbolic_ref_mutates_head(argv[2:])
            )
        )
    ):
        return f"Error: {branch_pinned_worktree_guidance(policy)}"
    return None


def _worktree_add_creates_branch(args: list[str]) -> bool:
    """Return whether worktree creation requests a new local branch."""
    if not args or args[0] != "add":
        return False
    return any(
        not option.startswith("--")
        and option.startswith("-")
        and any(flag in option[1:] for flag in "bB")
        for option in args[1:]
    )


def _worktree_add_creates_tracked_branch(
    args: list[str],
    repo: Path | None = None,
) -> bool:
    """Return whether worktree creation can rewrite shared branch config."""
    if not args or args[0] != "add":
        return False
    options = args[1:]
    creates_branch = _worktree_add_creates_branch(args)
    explicitly_tracks = any(
        option.startswith("--t")
        or option.startswith("--g")
        for option in options
    )
    if explicitly_tracks:
        return True
    if creates_branch:
        return "--no-track" not in options
    if any(option in {"-d", "--detach", "--orphan"} for option in options):
        return False

    operands = _worktree_path_args(options)
    if len(operands) < 2 or repo is None:
        # Path-only adds may create and track a branch when guessRemote is set
        # in repository config. Require an explicit, existing local branch.
        return True
    branch = operands[1]
    branch_ref = branch if branch.startswith("refs/heads/") else (
        f"refs/heads/{branch}"
    )
    try:
        return subprocess.run(
            [
                "git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                branch_ref,
            ],
            check=False,
            capture_output=True,
        ).returncode != 0
    except OSError:
        return True


def _direct_branch_mutation(subcommand: str, args: list[str]) -> bool:
    """Return whether a literal direct command changes base branch identity."""
    if subcommand in {"checkout", "switch"}:
        return True
    if subcommand == "branch":
        return _branch_mutates(args)
    if subcommand == "symbolic-ref":
        return _symbolic_ref_mutates_head(args)
    return False


def _resolve_literal_path(raw_path: str, cwd: Path | None) -> Path | None:
    if raw_path == "-" or any(
        marker in raw_path for marker in ("$", "`", "*", "?", "[", "]", "{")
    ):
        return None
    try:
        path = Path(raw_path).expanduser()
    except (KeyError, RuntimeError):
        return None
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _branch_mutates(args: list[str]) -> bool:
    """Return whether ``git branch`` creates or otherwise mutates a branch."""
    if not args:
        return False

    mutating_long_options = {
        "--copy",
        "--create-reflog",
        "--delete",
        "--edit-description",
        "--force",
        "--no-track",
        "--recurse-submodules",
        "--set-upstream-to",
        "--track",
        "--unset-upstream",
    }
    query_long_options = {
        "--all",
        "--contains",
        "--format",
        "--list",
        "--merged",
        "--no-contains",
        "--no-merged",
        "--points-at",
        "--remotes",
        "--show-current",
        "--sort",
    }
    for arg in args:
        option = arg.partition("=")[0]
        if option in mutating_long_options or (
            arg.startswith("-")
            and not arg.startswith("--")
            and any(flag in arg[1:] for flag in "cCdDftu")
        ):
            return True
    if any(
        arg.partition("=")[0] in query_long_options
        or (
            arg.startswith("-")
            and not arg.startswith("--")
            and any(flag in arg[1:] for flag in "alr")
        )
        for arg in args
    ):
        return False
    return any(not arg.startswith("-") for arg in args)


def _symbolic_ref_mutates_head(args: list[str]) -> bool:
    """Return whether ``git symbolic-ref`` writes or deletes ``HEAD``."""
    positionals: list[str] = []
    delete = False
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            positionals.extend(args[i + 1:])
            break
        if token in {"-m", "--message"}:
            i += 2
            continue
        if token.startswith("--message="):
            i += 1
            continue
        if token == "-d" or (
            token.startswith("--") and "--delete".startswith(token)
        ):
            delete = True
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        positionals.append(token)
        i += 1
    return bool(positionals) and positionals[0] == "HEAD" and (
        delete or len(positionals) >= 2
    )


def _registered_repo_for_path(path: Path, *, pinned_only: bool = False):
    """Resolve against this module's policy seam for testable shell guards."""
    return registered_repo_for_path(
        path,
        pinned_only=pinned_only,
        policies=list_registered_repo_policies(),
    )


def controlled_fast_forward_policy(
    command: str,
    cwd: str,
    *,
    policies: list[RepoPolicy] | None = None,
) -> RepoPolicy | None:
    """Return the policy for an exact base ``git pull --ff-only`` request."""
    if direct_argv(command) != ["git", "pull", "--ff-only"]:
        return None
    target = Path(cwd).expanduser().resolve(strict=False)
    policy = registered_repo_for_path(
        target,
        policies=(
            policies if policies is not None else list_registered_repo_policies()
        ),
    )
    if (
        policy is None
        or target != policy.path.resolve(strict=False)
        or not (policy.branch_pinned or policy.protect_base_checkout)
        or not policy.source_binding_valid
        or policy.enforcement_error
    ):
        return None
    return policy


def _is_within_path(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
        return True
    except ValueError:
        return False


def _worktree_path_args(args: list[str]) -> list[str]:
    """Return literal worktree path arguments, skipping known option values."""
    paths: list[str] = []
    j = 0
    while j < len(args):
        a = args[j]
        if a.startswith("-"):
            bundled_branch_needs_value = (
                not a.startswith("--")
                and any(flag in a[1:] for flag in "bB")
                and a[-1] in "bB"
            )
            if a in _WORKTREE_FLAGS_WITH_VALUE or bundled_branch_needs_value:
                j += 2
            else:
                j += 1
        else:
            paths.append(a)
            j += 1
    return paths


def _validate_worktree_target(target: str, cwd: str) -> str | None:
    p = Path(target).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        return None

    owner = _registered_repo_for_path(Path(cwd))
    if owner is not None:
        expected = owner.path.resolve(strict=False) / ".worktrees"
        if resolved != expected and _is_within_path(resolved, expected):
            return None
        return (
            "Error: registered-repository worktrees must be created under "
            f"{expected}/<name>."
        )

    for allowed in get_allowed_dirs_rw():
        try:
            resolved.relative_to(allowed.resolve())
            if "/.worktrees/" not in str(resolved) + "/":
                return "Error: worktrees must be created under `<repo>/.worktrees/<slug>`."
            return None
        except ValueError:
            continue

    return "Error: worktrees must be created under `<repo>/.worktrees/<slug>`."


def _sandbox_path(path: Path) -> str:
    return sandbox_literal(path.resolve(strict=False))


def _direct_git_command(
    command: str,
    cwd: str,
) -> tuple[Path, str, list[str]] | None:
    """Return a literal direct Git target, subcommand, and arguments."""
    argv = direct_argv(command)
    if not argv or argv[0] != "git" or "$" in command:
        return None
    target = Path(cwd).resolve(strict=False)
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "-C" and i + 1 < len(argv):
            resolved = _resolve_literal_path(argv[i + 1], target)
            if resolved is None:
                return None
            target = resolved
            i += 2
            continue
        if token in _SAFE_DIRECT_BRANCH_FLAGS:
            i += 1
            continue
        if token.startswith("-"):
            return None
        return target, token, argv[i + 1:]
    return None


def _git_dir_grant(repo: Path) -> str:
    """Return the scoped allow rule for a repository's ``.git`` metadata."""
    return (
        f'(allow file-read* file-write* '
        f'(subpath "{_sandbox_path(repo / ".git")}"))'
    )


def _verified_worktrees(policy: RepoPolicy, repo: Path) -> list[Path]:
    """Return linked worktrees whose reciprocal Git metadata proves ownership."""
    if not policy.source_binding_valid or policy.enforcement_error:
        return []
    return list_repo_worktrees(repo)


def pinned_repo_sandbox_profile(
    command: str, cwd: str,
    *,
    grant_policies: list[RepoPolicy] | None = None,
    controlled_policy: RepoPolicy | None = None,
    controlled_operation: str | None = None,
    policies: list[RepoPolicy] | None = None,
) -> str:
    """Build dynamic Safehouse rules for repository pins and protected bases."""
    source_policies = (
        policies if policies is not None else list_registered_repo_policies()
    )
    policies = [
        policy
        for policy in source_policies
        if (
            policy.branch_pinned
            or policy.protect_base_checkout
            or policy.enforcement_error
            or not policy.source_binding_valid
        )
    ]
    if controlled_policy is not None:
        live = [
            policy
            for policy in policies
            if policy.path.resolve(strict=False)
            == controlled_policy.path.resolve(strict=False)
        ]
        if len(live) != 1 or (
            live[0].pinned_branch,
            live[0].protect_base_checkout,
            live[0].enforcement_error,
            live[0].source_binding_valid,
        ) != (
            controlled_policy.pinned_branch,
            controlled_policy.protect_base_checkout,
            controlled_policy.enforcement_error,
            controlled_policy.source_binding_valid,
        ):
            raise ValueError(
                "repository policy changed during controlled fast-forward"
            )
        controlled_policy = live[0]
        if (
            not controlled_policy.source_binding_valid
            or controlled_policy.enforcement_error
        ):
            raise ValueError("repository policy is not enforceable")
    if not policies:
        return ""
    rules: list[str] = []
    seen: set[Path] = set()
    grant_repos = {
        policy.path.resolve(strict=False) for policy in (grant_policies or [])
        if policy.source_binding_valid and not policy.enforcement_error
    }
    verified_worktrees_by_repo: dict[Path, list[Path]] = {}
    for policy in policies:
        repo = policy.path.resolve(strict=False)
        if repo in seen:
            continue
        seen.add(repo)
        worktrees = _verified_worktrees(policy, repo)
        verified_worktrees_by_repo[repo] = worktrees
        if (
            policy.protect_base_checkout
            or policy.enforcement_error
            or not policy.source_binding_valid
        ):
            rules.append(f'(deny file-write* (subpath "{_sandbox_path(repo)}"))')
            for worktree in worktrees:  # resolved roots proved by this request's listing
                rules.append(
                    f'(allow file-read* file-write* (subpath "{sandbox_literal(worktree)}"))'
                )
    controlled_identity_write: set[Path] = set()
    worktree_control_repo: Path | None = None
    worktree_add_control_repo: Path | None = None
    if controlled_policy is not None:
        repo = controlled_policy.path.resolve(strict=False)
        if controlled_operation == "fetch":
            rules.append(_git_dir_grant(repo))
        elif controlled_operation == "merge":
            controlled_identity_write.add(repo)
            rules.append(
                f'(allow file-read* file-write* '
                f'(subpath "{_sandbox_path(repo)}"))'
            )
        elif controlled_operation != "query":
            raise ValueError("unsupported controlled fast-forward operation")
    direct = _direct_git_command(command, cwd) if controlled_policy is None else None
    if direct is not None:
        target, subcommand, args = direct
        policy = _registered_repo_for_path(target)
        if policy is not None and not (
            policy.branch_pinned
            or policy.protect_base_checkout
            or policy.enforcement_error
        ):
            policy = None
        if policy is not None and (
            not policy.source_binding_valid or policy.enforcement_error
        ):
            policy = None
        worktree_mutation = subcommand == "worktree" and bool(args) and (
            args[0] in _WORKTREE_MUTATION_ACTIONS
        )
        protected_new_branch = bool(
            policy is not None
            and policy.protect_base_checkout
            and worktree_mutation
            and args[0] == "add"
            and _worktree_add_creates_branch(args)
        )
        grantable_worktree_mutation = (
            worktree_mutation
            and not protected_new_branch
            and not _worktree_add_creates_tracked_branch(args, target)
        )
        if (
            policy is not None
            and repo_worktree_root(target, policy.path) is not None
            and not worktree_mutation
        ):
            repo = policy.path.resolve(strict=False)
            rules.append(_git_dir_grant(repo))
        elif policy is not None:
            repo = policy.path.resolve(strict=False)
            if subcommand == "fetch" and _is_allowed_base_fetch(args, repo):
                rules.append(_git_dir_grant(repo))
            elif grantable_worktree_mutation and target == repo:
                worktree_control_repo = repo
                if args[0] == "add":
                    worktree_add_control_repo = repo
                rules.extend(
                    [
                        _git_dir_grant(repo),
                        f'(allow file-read* file-write* '
                        f'(subpath "{_sandbox_path(repo / ".worktrees")}"))',
                        f'(allow file-read* file-write* '
                        f'(literal "{_sandbox_path(repo)}"))',
                    ]
                )
    elif controlled_policy is None:
        rules.extend(
            _git_dir_grant(repo) for repo, worktrees
            in verified_worktrees_by_repo.items() if worktrees and repo in grant_repos
        )
    for policy in policies:
        repo = policy.path.resolve(strict=False)
        protected = base_git_control_paths(repo)
        if repo == worktree_add_control_repo:
            # Git uses this transient lock while populating a linked worktree,
            # even when packed-refs itself is unchanged. The data file remains
            # denied; omitting only its lock avoids false failure diagnostics.
            protected.remove(repo / ".git" / "packed-refs.lock")
        if policy.protect_base_checkout and repo not in controlled_identity_write:
            protected.extend(base_git_identity_paths(repo))
        elif policy.branch_pinned or repo in controlled_identity_write:
            protected.extend(base_branch_selector_paths(repo))
        rules.extend(
            f'(deny file-write* (literal "{_sandbox_path(path)}"))'
            for path in protected
        )
        rules.append(
            f'(deny file-write* '
            f'(subpath "{_sandbox_path(repo / ".git" / "hooks")}"))'
        )
        if repo != worktree_control_repo:
            rules.extend(
                f'(deny file-write* '
                f'(literal "{sandbox_literal(worktree / ".git")}"))'
                for worktree in verified_worktrees_by_repo.get(repo, ())  # .git proved a plain file
            )
    for unsupported in list_unsupported_pinned_repos(policies):
        rules.extend(
            f'(deny file-write* (subpath "{_sandbox_path(root)}"))'
            for root in unsupported.metadata_roots
        )
    return "\n".join(rules) + "\n"


def _is_allowed_base_fetch(args: list[str], repo: Path) -> bool:
    """Return whether pinned-base fetch args are bare or a configured remote."""
    return not args or (
        len(args) == 1 and args[0] in _configured_remote_names(repo)
    )


def _configured_remote_names(repo: Path) -> set[str]:
    """Return remote names configured in ``repo``'s own local Git config.

    Uses ``--local`` (not ``git remote``/``--includes`` alone) so a remote
    defined only in the user's global or system Git config can't be treated
    as configured for this repo; ``--includes`` still follows this repo's
    own ``include.path``/``includeIf`` directives.
    """
    try:
        result = subprocess.run(
            [
                "git", "config", "--local", "--includes",
                "--get-regexp", r"^remote\..*\.url$",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode not in (0, 1):
        return set()
    names: set[str] = set()
    for line in result.stdout.splitlines():
        key = line.split(" ", 1)[0]
        if key.startswith("remote.") and key.endswith(".url"):
            names.add(key[len("remote."):-len(".url")])
    return names


_COMPOSED_SEGMENT_SEPARATORS = frozenset(
    {";", "&&", "||", "|", "|&", "&", "\n"}
)

_CHAINED_BASE_GIT_GUIDANCE = (
    "git works one command at a time on a protected base checkout — rerun as a "
    "single literal git command."
)


def _split_top_level_segments(command: str) -> list[str]:
    """Split a composed command into its top-level command segments.

    Segments are the pieces separated by shell control operators (``&&``,
    ``||``, ``;``, ``|``, ``&``). Quoting is respected via the shared tokenizer.
    This is advisory-only parsing for recovery guidance, never a security
    boundary — Safehouse remains the enforcement backstop.
    """
    tokens = command_tokens(command)
    if not tokens:
        return []
    segments: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in _COMPOSED_SEGMENT_SEPARATORS:
            if current:
                segments.append(shlex.join(current))
                current = []
            continue
        current.append(token)
    if current:
        segments.append(shlex.join(current))
    return segments


def _segment_is_base_grantable_git(segment: str, cwd: str) -> bool:
    """Return whether one segment is a Git op the base checkout grants alone.

    These are exactly the narrow operations a protected or branch-pinned base
    checkout permits as a single literal command: a bare or configured-remote
    ``git fetch``, ``git pull --ff-only``, and a scoped ``git worktree add``.
    Their presence in a composed command is what makes unchaining the fix.
    """
    direct = _direct_git_command(segment, cwd)
    if direct is None:
        return False
    target, subcommand, args = direct
    policy = _registered_repo_for_path(target)
    if policy is None or not (
        policy.branch_pinned or policy.protect_base_checkout
    ):
        return False
    if not policy.source_binding_valid or policy.enforcement_error:
        return False
    repo = policy.path.resolve(strict=False)
    if repo_worktree_root(target, policy.path) is not None:
        return False
    if subcommand == "fetch" and _is_allowed_base_fetch(args, repo):
        return True
    if subcommand == "pull" and args == ["--ff-only"] and target == repo:
        return True
    return (
        subcommand == "worktree"
        and bool(args)
        and args[0] == "add"
        and target == repo
        and not (
            policy.protect_base_checkout
            and _worktree_add_creates_branch(args)
        )
        and not _worktree_add_creates_tracked_branch(args, target)
    )


def chained_base_git_denial_guidance(command: str, cwd: str) -> str | None:
    """Teach the one-literal-command rule for chained Git on a protected base.

    Verified-worktree inventory grants ordinary Git independently of cwd, so
    this residual guidance is scoped to the base checkout: a composed command
    (never a single literal Git
    invocation) whose working directory is a protected or branch-pinned base
    checkout, and that contains at least one Git operation the base checkout
    grants on its own (``fetch`` / ``pull --ff-only`` / ``worktree add``). For
    those, unchaining resolves the denial, so the teaching is accurate. It is
    advisory recovery text appended only after Safehouse has already denied the
    write; it never grants access.
    """
    if _direct_git_command(command, cwd) is not None:
        return None
    location = Path(cwd).expanduser().resolve(strict=False)
    policy = _registered_repo_for_path(location)
    if policy is None or not (
        policy.branch_pinned or policy.protect_base_checkout
    ):
        return None
    if repo_worktree_root(location, policy.path) is not None:
        return None
    if any(
        _segment_is_base_grantable_git(segment, cwd)
        for segment in _split_top_level_segments(command)
    ):
        return _CHAINED_BASE_GIT_GUIDANCE
    return None


def _canonical_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    canonical: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            canonical.append(resolved)
    return canonical


def _safehouse_executable() -> Path:
    executable = shutil.which("safehouse")
    if executable is None:
        raise FileNotFoundError("Agent Safehouse is unavailable")
    return Path(executable).expanduser().resolve(strict=False)


def safehouse_argv(
    command: str,
    cwd: str | Path,
    append_profile: str | Path,
    *,
    leased: bool = False,
    external_agent: bool = False,
    agent_account: AgentAccountView | None = None,
    thread_id: str | None = None,
    policies: list[RepoPolicy] | None = None,
) -> list[str]:
    """Build one direct Safehouse argv from the spawn's account view."""
    canonical_cwd = Path(cwd).expanduser().resolve(strict=False)
    canonical_profile = Path(append_profile).expanduser().resolve(strict=False)
    argv = [
        str(_safehouse_executable()),
        f"--workdir={canonical_cwd}" if leased else "--workdir=",
        "--trust-workdir-config=false",
    ]
    if leased:
        # Safehouse 0.11.1 otherwise installs a terminal deny for
        # ``<workdir>/.safehouse``. Workdir configuration is explicitly
        # untrusted below, so that file is not live lease authority.
        argv.append("--allow-workdir-config-writes")
    else:
        if rw := ":".join(map(str, _canonical_paths(get_sandbox_add_dirs_rw(thread_id, policies=policies)))):
            argv.append(f"--add-dirs={rw}")
        read_only = [*get_allowed_dirs_ro(policies=policies), *agent_account_ro_dirs(agent_account), *agent_tool_ro_dirs()]
        if ro := ":".join(map(str, _canonical_paths(read_only))):
            argv.append(f"--add-dirs-ro={ro}")
    argv.extend(
        [
            f"--enable={'docker' if leased else 'ssh'},agent-browser",
            f"--append-profile={canonical_profile}",
            f"--env-pass={shell_env_pass_names()}",
            "--",
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            command,
        ]
    )
    return argv


def _terminal_boundary_profile(
    profile_path: Path,
    cwd: Path,
    *,
    leased: bool,
    external_agent: bool = False,
    agent_account: AgentAccountView | None = None,
    thread_id: str | None = None,
) -> str:
    """Render the protected or reviewed lease boundary."""
    generated_profile = profile_path.expanduser().resolve(strict=False)
    selected_safehouse = Path(shutil.which("safehouse") or "safehouse")
    safehouse_paths = _canonical_paths([selected_safehouse, _safehouse_executable()])
    if leased:
        # Connected-provider tool confinement is established structurally at
        # the Claude Code and Codex launch seams, not by mutable path matching.
        direct_denied_executables: list[Path] = []
    else:
        direct_denied_executables = _canonical_paths(
            [
                Path("/usr/bin/sudo"),
                Path("/usr/bin/su"),
                Path("/usr/local/bin/doas"),
                Path("/opt/homebrew/bin/doas"),
                Path("/sbin/shutdown"),
                Path("/sbin/reboot"),
                Path("/sbin/halt"),
                Path("/sbin/poweroff"),
                Path("/usr/bin/pmset"),
            ]
        )
    path_policy = current_protected_host_path_policy(
        generated_profile=generated_profile, safehouse_paths=safehouse_paths
    )
    lines = [
        ";; CORSIA LEASE INVARIANTS" if leased
        else ";; CORSIA PROTECTED TERMINAL DENIES"
    ]
    if leased:
        lines.extend(
            [
                # Invariant 1: the process-private confinement cannot rewrite
                # its appended profile or replace/re-enter Safehouse.
                (f'(deny file-write* (literal "{_sandbox_path(generated_profile)}"))'),
                *(
                    f'(deny file-write* process-exec (literal "{_sandbox_path(path)}"))'
                    for path in safehouse_paths
                ),
                path_policy.hard_floor_sandbox_rules(),
            ]
        )
        if external_agent:
            lines.append(external_agent_sandbox_rules(generated_profile, safehouse_paths))
    else:
        lines += [path_policy.sandbox_rules(), _NO_CLIPBOARD, unix_socket_sandbox_rules(thread_id)]
        # While the agents' account is in use, the user's own gh credentials are out of reach.
        if (rule := agent_account_deny_rule(_sandbox_path, agent_account)) is not None:
            lines.append(rule)
    lines.extend(
        [
            # Invariants 1 and 2: the command can manage its own subprocesses,
            # but cannot inspect/control the backend or another thread's jail.
            "(deny process-info*)",
            "(deny signal)",
            "(deny mach-priv-task-port)",
            "(allow process-info* (target same-sandbox))",
            "(allow signal (target same-sandbox))",
            "(allow mach-priv-task-port (target same-sandbox))",
            *(
                f'(deny process-exec (literal "{_sandbox_path(path)}"))'
                for path in direct_denied_executables
            ),
        ]
    )
    if not leased:
        # --workdir= skips Safehouse's config deny; retain it after all grants.
        config_path = _sandbox_path(cwd / ".safehouse")
        lines.append(f'(deny file-write* (literal "{config_path}"))')
    return "\n".join(lines) + "\n"


def execution_envelope_profile(
    command: str,
    cwd: str | Path,
    profile_path: str | Path,
    *,
    leased: bool,
    external_agent: bool = False,
    grant_policies: list[RepoPolicy] | None = None,
    controlled_policy: RepoPolicy | None = None,
    controlled_operation: str | None = None,
    policies: list[RepoPolicy] | None = None,
    agent_account: AgentAccountView | None = None,
    thread_id: str | None = None,
) -> str:
    """Render protected or reviewed-leased rules around one repo renderer."""
    canonical_cwd = Path(cwd).expanduser().resolve(strict=False)
    canonical_profile = Path(profile_path).expanduser().resolve(strict=False)
    terminal_rules = _terminal_boundary_profile(
        canonical_profile,
        cwd=canonical_cwd,
        leased=leased,
        external_agent=external_agent,
        agent_account=agent_account,
        thread_id=thread_id,
    )
    if leased:
        return "(allow default)\n" + terminal_rules
    repository_rules = pinned_repo_sandbox_profile(
        command,
        str(canonical_cwd),
        controlled_policy=controlled_policy,
        controlled_operation=controlled_operation,
        grant_policies=grant_policies,
        policies=policies,
    )
    return repository_rules + terminal_rules


def safehouse_version_error() -> str | None:
    """Return a fail-closed error for an unverified or vulnerable Safehouse."""
    try:
        result = subprocess.run(
            [str(_safehouse_executable()), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=safehouse_subprocess_env(None),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return f"could not verify the Agent Safehouse version: {exc}"
    if result.returncode != 0:
        return "could not verify the Agent Safehouse version"

    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if match is None:
        return "could not determine the Agent Safehouse version"
    version = tuple(int(part) for part in match.groups())
    if version < _MIN_SAFEHOUSE_VERSION:
        found = ".".join(str(part) for part in version)
        return (
            "Agent Safehouse 0.11.1 or newer is required "
            f"(found {found})"
        )
    return None


def is_sandbox_denial(
    stderr: str,
    is_available: AvailabilityCheck = is_safehouse_available,
) -> bool:
    """Return true when stderr looks like a Safehouse filesystem denial."""
    return bool(stderr) and "Operation not permitted" in stderr and is_available()
