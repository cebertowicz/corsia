"""Confinement and private-cache setup for local MCP stdio transports."""

import hashlib
import os
import re
import shlex
import tempfile
from pathlib import Path

from tools.host_path_resolution import denied_paths_in
from tools.mcp_config import MCPConfigError


_MCP_DENIED_PATH = re.compile(
    r'''(?ix)
    (?:operation\s+not\s+permitted|eperm)
    [^'"\r\n]*?
    (?P<quote>['"])(?P<path>/[^'"\r\n]+)(?P=quote)
    ''',
)
_SAFEHOUSE_INHERITED_ENV = {"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"}


def _mcp_denied_paths_in(text: str) -> tuple[Path, ...]:
    paths = (
        *denied_paths_in(text),
        *(Path(match.group("path")) for match in _MCP_DENIED_PATH.finditer(text)),
    )
    return tuple(dict.fromkeys(paths))


def _mcp_app_id(name: str) -> str:
    """Return a stable path-safe identity for one configured MCP app."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _mcp_cache_dir(name: str, workspace: Path, state_root: Path) -> Path:
    """Return an app-private cache that cannot overlap CORSIA workspace or state."""
    from infra.platform_paths import APPLICATION_ID

    cache_dir = (Path.home() / "Library" / "Caches" / APPLICATION_ID / "mcp" / name).resolve(strict=False)
    if any(
        cache_dir == root or cache_dir.is_relative_to(root) or root.is_relative_to(cache_dir)
        for root in (workspace.resolve(strict=False), state_root.resolve(strict=False))
    ):
        raise MCPConfigError("local MCP cache must be outside CORSIA workspace and state")
    return cache_dir


def _mcp_denial_guidance(text: str) -> str | None:
    """Clarify only path-aware denials imposed by the MCP private-state profile."""
    from tools.host_path_policy import current_protected_host_path_policy

    guidance = current_protected_host_path_policy().denial_guidance_for_paths(_mcp_denied_paths_in(text))
    if not guidance:
        return None
    path, separator, message = guidance.partition(" — ")
    if not separator:
        return None
    if "requires reviewed host access" in message:
        return (
            f"{path} — CORSIA private state is blocked for local MCP apps. Nothing was returned. "
            "Use the app's own working directory or a supported non-private integration."
        )
    if "CORSIA and Safehouse private state is blocked" in message or "Stored secrets are closed" in message:
        return guidance
    return None


def _mcp_setup_refusal(detail: str) -> str:
    return f"local MCP app confinement setup failed: {detail}. Nothing ran."


def mcp_oserror_detail(error: OSError) -> str:
    """Preserve non-private launch errors while clarifying private-state denials."""
    return _mcp_denial_guidance(str(error)) or str(error)


def safehouse_stdio_parameters(name: str, config: dict, command: str, args: list[str], env: dict[str, str]):
    """Build a fail-closed direct-exec Safehouse launch for one local app."""
    from mcp import StdioServerParameters

    from infra.config import STATE_ROOT, WORKSPACE
    from tools.safehouse_launch import direct_safehouse_argv, mcp_envelope_dir, mcp_private_state_profile, write_no_proctitle_shim
    from tools.shell_guard import _safehouse_executable, safehouse_version_error

    if version_error := safehouse_version_error():
        if "unavailable" in version_error.lower():
            from host_access.refusal_messages import safehouse_unavailable_refusal

            raise MCPConfigError(safehouse_unavailable_refusal(os.environ.get("PATH")))
        raise MCPConfigError(_mcp_setup_refusal(version_error))
    workspace = Path(WORKSPACE).expanduser().resolve(strict=False)
    cache_identity = config.get("connected_app_id") or _mcp_app_id(name)
    if not isinstance(cache_identity, str) or not cache_identity:
        raise MCPConfigError("connected app identity must be a nonempty string")
    cache_dir = _mcp_cache_dir(cache_identity, workspace, Path(STATE_ROOT))
    cwd = cache_dir / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    shim_path = write_no_proctitle_shim(cache_dir)
    node_options = f"--require {shlex.quote(str(shim_path))}"
    if existing := env.get("NODE_OPTIONS"):
        node_options = f"{node_options} {existing}"
    server_env = {
        "npm_config_script_shell": "/bin/sh",
        **env,
        "npm_config_cache": str(cache_dir / "npm"),
        "UV_CACHE_DIR": str(cache_dir / "uv"),
        "XDG_CACHE_HOME": str(cache_dir / "xdg"),
        "NODE_OPTIONS": node_options,
    }
    descriptor, raw_profile = tempfile.mkstemp(
        dir=mcp_envelope_dir(Path(STATE_ROOT)), prefix="corsia-mcp-envelope-", suffix=".sb",
    )
    profile_path = Path(raw_profile).resolve(strict=False)
    try:
        safehouse_path = _safehouse_executable()
        with os.fdopen(descriptor, "w", encoding="utf-8") as profile:
            profile.write(mcp_private_state_profile(profile_path, safehouse_path, Path(STATE_ROOT)))
        argv = direct_safehouse_argv(
            command, args, cwd, profile_path,
            env_pass_names=set(server_env) | _SAFEHOUSE_INHERITED_ENV,
            executable=lambda: safehouse_path,
        )
    except (OSError, ValueError) as error:
        profile_path.unlink(missing_ok=True)
        raise MCPConfigError(_mcp_setup_refusal(type(error).__name__)) from None
    return StdioServerParameters(command=argv[0], args=argv[1:], env=server_env, cwd=cwd), profile_path
