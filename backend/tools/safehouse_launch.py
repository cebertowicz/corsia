"""Shared Safehouse argv construction for protected child launches."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from connections.github_agent import AgentAccountView
from infra.project_repo_policy import RepoPolicy

def _canonical_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    canonical: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            canonical.append(resolved)
    return canonical


def safehouse_prefix(
    cwd: str | Path,
    append_profile: str | Path,
    env_pass_names: str,
    *,
    executable: Callable[[], Path],
    sandbox_dirs_rw: Callable[..., list[Path]],
    allowed_dirs_ro: Callable[..., list[Path]],
    agent_account_dirs: Callable[[AgentAccountView | None], list[Path]],
    leased: bool,
    agent_account: AgentAccountView | None = None,
    include_agent_account: bool = True,
    gws_home: Path | None = None,
    thread_id: str | None = None,
    policies: list[RepoPolicy] | None = None,
) -> list[str]:
    """Build the options shared by shell and direct-exec Safehouse launches."""
    canonical_cwd = Path(cwd).expanduser().resolve(strict=False)
    canonical_profile = Path(append_profile).expanduser().resolve(strict=False)
    argv = [
        str(executable()),
        f"--workdir={canonical_cwd}" if leased else "--workdir=",
        "--trust-workdir-config=false",
    ]
    if leased:
        argv.append("--allow-workdir-config-writes")
    else:
        rw_paths = [*sandbox_dirs_rw(thread_id, policies=policies)]
        if gws_home:
            rw_paths.append(gws_home)
        if rw := ":".join(map(str, _canonical_paths(rw_paths))):
            argv.append(f"--add-dirs={rw}")
        read_only = [*allowed_dirs_ro(policies=policies)]
        if include_agent_account:
            read_only.extend(agent_account_dirs(agent_account))
        if ro := ":".join(map(str, _canonical_paths(read_only))):
            argv.append(f"--add-dirs-ro={ro}")
    return [
        *argv,
        f"--enable={'docker' if leased else 'ssh'},agent-browser",
        f"--append-profile={canonical_profile}",
        f"--env-pass={env_pass_names}",
        "--",
    ]


def direct_safehouse_argv(
    command: str,
    args: list[str],
    cwd: str | Path,
    append_profile: str | Path,
    *,
    env_pass_names: set[str],
    executable: Callable[[], Path],
    sandbox_dirs_rw: Callable[..., list[Path]],
    allowed_dirs_ro: Callable[..., list[Path]],
    agent_account_dirs: Callable[[AgentAccountView | None], list[Path]],
) -> list[str]:
    """Build a scope-neutral Safehouse argv that execs an app without a shell."""
    if not command or any(not isinstance(arg, str) for arg in args):
        raise ValueError("Safehouse direct exec requires a command and string arguments")
    return [
        *safehouse_prefix(
            cwd,
            append_profile,
            ",".join(sorted(env_pass_names)),
            executable=executable,
            sandbox_dirs_rw=sandbox_dirs_rw,
            allowed_dirs_ro=allowed_dirs_ro,
            agent_account_dirs=agent_account_dirs,
            leased=False,
            include_agent_account=False,
        ),
        command,
        *args,
    ]
