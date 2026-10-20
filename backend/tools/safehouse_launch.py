"""Direct-exec Safehouse launch construction for confined child applications."""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path


def direct_safehouse_argv(
    command: str,
    args: list[str],
    cwd: str | Path,
    append_profile: str | Path,
    *,
    env_pass_names: set[str],
    executable: Callable[[], Path],
) -> list[str]:
    """Build an allow-by-default direct-exec launch with an appended profile."""
    if not command or any(not isinstance(arg, str) for arg in args):
        raise ValueError("Safehouse direct exec requires a command and string arguments")
    canonical_cwd = Path(cwd).expanduser().resolve(strict=False)
    canonical_profile = Path(append_profile).expanduser().resolve(strict=False)
    return [
        str(executable()),
        f"--workdir={canonical_cwd}",
        "--trust-workdir-config=false",
        "--allow-workdir-config-writes",
        f"--append-profile={canonical_profile}",
        f"--env-pass={','.join(sorted(env_pass_names))}",
        "--",
        command,
        *args,
    ]


def mcp_envelope_dir(state_root: Path) -> Path:
    """Return the state-owned, agent-denied directory for MCP profiles."""
    profile_dir = state_root / "runtime" / "mcp-envelopes"
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_dir.chmod(0o700)
    return profile_dir


_NO_PROCTITLE_SHIM = """const descriptor = Object.getOwnPropertyDescriptor(process, 'title');
if (!descriptor?.configurable) {
  throw new Error('CORSIA MCP preload cannot safely disable process.title');
}
const get = typeof descriptor.get === 'function' ? descriptor.get : () => descriptor.value;
Object.defineProperty(process, 'title', {get, set(){}, configurable:true});
"""


def write_no_proctitle_shim(cache_dir: Path) -> Path:
    """Write the app-local Node preload before a Safehouse launch."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    shim_path = cache_dir / "no-proctitle.cjs"
    shim_path.write_text(_NO_PROCTITLE_SHIM, encoding="utf-8")
    return shim_path


def _generated_profile_temp_rules() -> tuple[str, ...]:
    from tools.host_path_policy import (
        GENERATED_PROFILE_PREFIXES,
        _sandbox_regex,
        generated_profile_temp_roots,
    )

    return tuple(
        f'(deny file-write* (regex #"^{_sandbox_regex(str(root))}/{prefix}[^/]*\\.sb$"))'
        for root in generated_profile_temp_roots()
        for prefix in GENERATED_PROFILE_PREFIXES
    )


def mcp_private_state_profile(
    profile_path: Path,
    safehouse_path: Path,
    state_root: Path,
    cache_dir: Path,
) -> str:
    """Render the allow-default profile from the shared protected-path inventory."""
    # Temporary floor block until a reviewed host-access path for MCP exists (#154).
    from tools.host_path_policy import current_protected_host_path_policy

    policy = current_protected_host_path_policy(
        generated_profile=profile_path,
        safehouse_paths=(safehouse_path,),
    )

    def sbpl_path(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    lexical_state_root = Path(os.path.abspath(str(state_root.expanduser())))
    denied_roots = tuple(dict.fromkeys((lexical_state_root, policy.state_root, *policy.private_roots)))
    path_rules = [
        f'(deny file-read* file-write* (subpath "{sbpl_path(path)}"))'
        for path in denied_roots
    ]
    path_rules.append(policy.hard_floor_sandbox_rules())
    # Local apps execute this cache but can never replace its verified runtime
    # siblings or parent. Reopen only the launched app's private cache directory.
    from tools.managed_runtime import application_cache_root

    path_rules.extend((
        f'(deny file-write* (subpath "{sbpl_path(application_cache_root())}"))',
        f'(allow file-write* (subpath "{sbpl_path(cache_dir)}"))',
    ))
    if policy.generated_profile is not None:
        path_rules.append(
            f'(deny file-read* file-write* (literal "{sbpl_path(policy.generated_profile)}"))'
        )
    path_rules.extend(
        f'(deny file-write* process-exec (literal "{sbpl_path(path)}"))'
        for path in policy.safehouse_paths
    )
    return "\n".join(
        [
            "(allow default)",
            ";; Temporary MCP private-state floor block; remove after #154.",
            *path_rules,
            *_generated_profile_temp_rules(),
            "(deny process-info*)",
            "(deny signal)",
            "(deny mach-priv-task-port)",
            "(allow process-info* (target same-sandbox))",
            "(allow signal (target same-sandbox))",
            "(allow mach-priv-task-port (target same-sandbox))",
        ]
    ) + "\n"
