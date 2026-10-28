"""Native CLI attempts and producer protocol; no daemon lifecycle policy."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import IO, Literal, Sequence

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from host_access import browser_identity as identity
from infra.platform_paths import app_bundle_root

logger = logging.getLogger(__name__)
_PENDING_CLIENTS: set[asyncio.Task] = set()
_PRE_DAEMON_ERRORS = frozenset({
    "unknown_command", "unknown_subcommand", "missing_arguments",
    "invalid_value", "invalid_session_name",
})
BROWSER_COMMAND = "agent-browser"
LINUX_CHROME_PATHS = (Path("/opt/google/chrome/chrome"), Path("/opt/corsia/chromium/chrome"))
_SOURCE_INSTALL_HINT = (
    "install a supported build with `npm install -g agent-browser@0.31`, "
    "run `agent-browser install`, and make sure PATH selects it"
)
_PACKAGED_INSTALL_HINT = "reinstall or update CORSIA to restore its bundled copy"
WAIT_TIMED_OUT = "Wait timed out; the browser session is still running. Take a snapshot to see the page."
BROWSER_BUSY = "Browser timed out or is busy with an earlier command; the session is still running. Take a snapshot when it finishes."
CHROME_INSTALL_TIMEOUT = 600.0
# Each family's producer contract was verified against the real native build.
_VERIFIED_FAMILIES = (">=0.27.0,<0.28.0", ">=0.31.0,<0.32.0")


@dataclass(frozen=True, slots=True)
class NativeExecutable:
    """Immutable admitted canonical executable and inspected version."""
    path: str
    fingerprint: str
    version: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Client completion is distinct from daemon effects; pending work stays owned."""
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    completion: Literal["exited", "interrupted", "unknown"] = "exited"
    settlement: asyncio.Task[bool] | None = field(default=None, repr=False, compare=False)

    def settled(self) -> CommandResult:
        """Consume a late client's reap result without inventing no-daemon evidence."""
        if self.settlement is not None and self.settlement.done():
            if not self.settlement.cancelled() and self.settlement.result():
                return replace(self, completion="interrupted", settlement=None)
        return self

    def rejected_before_daemon(self) -> bool:
        """Recognize only native main's typed parse rejection before ensure_daemon."""
        if self.completion != "exited" or self.timed_out or self.returncode != 1:
            return False
        payload = json_object(self.stdout)
        return (payload.get("success") is False
                and isinstance(payload.get("error"), str)
                and isinstance(payload.get("errorType"), str)
                and payload["errorType"] in _PRE_DAEMON_ERRORS)


class CommandCancelled(asyncio.CancelledError):
    """Cancellation carrying the client producer's actual completion evidence."""
    def __init__(self, result: CommandResult) -> None:
        super().__init__()
        self.result = result


def admitted_version(output: str) -> str:
    """Admit a conservative patch family; overrides require protocol verification."""
    match = re.fullmatch(r"agent-browser\s+v?(\d+\.\d+\.\d+)\s*", output.strip())
    override = os.environ.get("CORSIA_AGENT_BROWSER_VERSION_RANGE")
    policies = _VERIFIED_FAMILIES if override is None else (override,)
    policy = " or ".join(policies)
    try:
        if match is not None and any(p.strip() and Version(match[1]) in SpecifierSet(p)
                                     for p in policies):
            return match[1]
    except InvalidSpecifier as exc:
        raise identity.BrowserIdentityError(
            "browser version policy is invalid; fix or unset CORSIA_AGENT_BROWSER_VERSION_RANGE"
        ) from exc
    found = f"agent-browser {match[1]} is" if match is not None else "the installed agent-browser reports an unrecognized version and is"
    raise identity.BrowserIdentityError(
        f"{found} not a supported native agent-browser version ({policy}); it is {_origin()}; {install_hint()}"
    )


def bundled_executable() -> str | None:
    """The agent-browser shipped in this app bundle, or None outside one."""
    bundle = app_bundle_root()
    if bundle is None:
        return None
    executable = bundle / "Contents" / "Resources" / "bin" / BROWSER_COMMAND
    return str(executable) if executable.is_file() else None


def install_hint() -> str:
    """How to get a supported agent-browser: a packaged app never sends anyone to npm."""
    return _PACKAGED_INSTALL_HINT if app_bundle_root() is not None else _SOURCE_INSTALL_HINT


def _origin() -> str:
    """Which copy a rejection is about: a packaged app runs only its own, a source install uses PATH."""
    return "the agent-browser bundled with this app" if app_bundle_root() is not None else "the agent-browser on PATH"


def _not_installed() -> str:
    if app_bundle_root() is not None:
        return f"agent-browser is missing from this app; {install_hint()}"
    return f"agent-browser is not installed or not on PATH; {install_hint()}"


def _application_dirs() -> tuple[Path, ...]:
    return Path("/Applications"), Path.home() / "Applications"


def chrome_executable() -> tuple[str | None, Literal["env", "system", "default"]]:
    """Select Chrome; Mac prefers installed Chrome because Chrome for Testing 152
    can stop streaming frames. None keeps agent-browser's own download."""
    if sys.platform == "linux":
        for candidate in LINUX_CHROME_PATHS:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate), "system"
        raise identity.BrowserIdentityError("The browser is missing. Re-run the CORSIA installer.")
    if explicit := os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH"):
        return explicit, "env"
    for folder in _application_dirs():
        candidate = folder / "Google Chrome.app/Contents/MacOS/Google Chrome"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), "system"
    return None, "default"


def downloaded_chrome_executable() -> str | None:
    """Resolve agent-browser's installed Chrome after its installer completes."""
    root = Path.home() / ".agent-browser" / "browsers"
    candidates = sorted(root.glob("chrome-*/**/Google Chrome for Testing"))
    if not candidates:
        candidates = sorted(root.glob("chrome-*/**/chrome"))
    return next((str(path) for path in reversed(candidates) if path.is_file() and os.access(path, os.X_OK)), None)


def chrome_download_missing() -> bool:
    """Whether agent-browser has no Chrome for Testing in the person's home yet."""
    # Tied to the layout of the bundled agent-browser 0.31.2; recheck it when that pin moves.
    return next(iter((Path.home() / ".agent-browser" / "browsers").glob("chrome-*")), None) is None


def resolve_native(binary: str) -> tuple[str, str]:
    """Reject scripts/wrappers before invoking even --version; follow symlinks.

    A packaged app runs only its own copy; a source install uses PATH.
    """
    selected: str | None
    if binary == BROWSER_COMMAND and app_bundle_root() is not None:
        selected = bundled_executable()
    else:
        selected = shutil.which(binary) if not Path(binary).is_absolute() else binary
    if selected is None:
        raise identity.BrowserIdentityError(_not_installed())
    try:
        path = Path(selected).resolve(strict=True)
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
            raise ValueError
        with path.open("rb") as source:
            magic = source.read(4)
        if magic not in {b'\x7fELF', b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                         b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                         b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                         b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}:
            raise ValueError
        return identity.executable_fingerprint_for_path(path)
    except FileNotFoundError as exc:
        raise identity.BrowserIdentityError(_not_installed()) from exc
    except (OSError, ValueError) as exc:
        raise identity.BrowserIdentityError(
            "agent-browser must resolve to the native agent-browser executable, "
            f"not a script or launcher; it is {_origin()}; {install_hint()}"
        ) from exc


def json_object(raw: str) -> dict:
    """Decode exactly one object; partial/noisy output is never protocol evidence."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def render_model_result(result: CommandResult, *, redactions: dict[str, str]) -> str:
    """Preserve readable tool strings while redacting private ownership facts."""
    if result.timed_out or result.completion == "interrupted":
        return f"Error: {BROWSER_BUSY}"
    payload = json_object(result.stdout)
    if result.returncode != 0:
        # The native --json error (or stderr) is what the agent can act on.
        error = payload.get("error")
        detail = error.strip() if isinstance(error, str) and error.strip() else result.stderr.strip()
        return _bounded(f"Error: agent-browser command failed: {detail}" if detail
                        else "Error: agent-browser command failed.", redactions)
    data = payload.get("data")
    if isinstance(data, dict):
        data = {key: value for key, value in data.items() if key != "lifecycle"}
        if isinstance(data.get("snapshot"), str):
            rendered = data["snapshot"]
        elif "result" in data:
            rendered = data["result"] if isinstance(data["result"], str) else json.dumps(data["result"])
        elif "title" in data and "url" in data:
            rendered = f"{data['title']}\n{data['url']}"
        elif "path" in data:
            rendered = f"Screenshot saved to {data['path']}"
        else:
            rendered = json.dumps(data, ensure_ascii=False) if data else ""
    else:
        rendered = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return _bounded(rendered, redactions)


def _bounded(rendered: str, redactions: dict[str, str]) -> str:
    for private_value, label in redactions.items():
        if private_value:
            rendered = rendered.replace(private_value, label)
    return (rendered[:8000] + "\n\n... [output truncated at 8,000 chars]"
            if len(rendered) > 8000 else rendered or "(no output)")


async def _stop_client(process: asyncio.subprocess.Process) -> bool:
    try:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5.0)
        return True
    except ProcessLookupError:
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _retain(task: asyncio.Task) -> asyncio.Task:
    _PENDING_CLIENTS.add(task)
    task.add_done_callback(_PENDING_CLIENTS.discard)
    return task


async def _settle_late_spawn(spawn: asyncio.Task) -> bool:
    try:
        return await _stop_client(await spawn)
    except Exception as exc:
        _diagnostic(exc, "late_spawn")
        return False


def _diagnostic(exc: Exception, phase: str) -> str:
    """Log fixed labels, never exception text, private inputs or exception names."""
    cause = "unexpected_error"
    for error_type, label in ((PermissionError, "permission_error"),
                              (FileNotFoundError, "not_found"),
                              (OSError, "os_error"), (RuntimeError, "runtime_error")):
        if isinstance(exc, error_type):
            cause = label
            break
    diagnostic = f"browser client failure: phase={phase}; cause={cause}"
    logger.warning("%s", diagnostic)
    return diagnostic


def _read_output(stream: IO[bytes]) -> str:
    stream.seek(0)
    return stream.read().decode("utf-8", errors="replace").strip()


async def invoke(argv: Sequence[str], *, socket_dir: Path, timeout: float,
                 stream_port: int | None = None, config_path: Path | None = None,
                 home: Path | None = None, linux_chrome: str | None = None) -> CommandResult:
    """Settle the actual subprocess; unknown effects are never a no-launch claim."""
    from host_access.browser_environment import client_environment
    env = client_environment(os.environ, socket_dir, stream_port)
    if sys.platform == "linux":
        env.pop("CI", None)  # Native agent-browser otherwise disables Chromium's sandbox.
        if linux_chrome:
            env["AGENT_BROWSER_EXECUTABLE_PATH"] = linux_chrome
    if config_path is not None:
        argv = (argv[0], "--config", str(config_path), *argv[1:])
    if home is not None:
        env["HOME"] = str(home)
    # The client starts a browser daemon that outlives it, and a descendant can keep the client's stdout and stderr
    # open after the client exits. Pipes would then never reach end-of-file, so the client writes to files and
    # settles on its own exit; what a descendant writes later goes to a file nobody waits on.
    output, errors = tempfile.TemporaryFile(), tempfile.TemporaryFile()

    def release_output(_: object = None) -> None:
        output.close()
        errors.close()

    spawn = _retain(asyncio.create_task(asyncio.create_subprocess_exec(
        *tuple(argv), stdout=output, stderr=errors, env=env,
    )))
    try:
        process = await asyncio.wait_for(asyncio.shield(spawn), timeout)
        if (current := asyncio.current_task()) is not None and current.cancelling():
            raise asyncio.CancelledError
    except asyncio.TimeoutError:
        spawn.add_done_callback(release_output)
        pending = _retain(asyncio.create_task(_settle_late_spawn(spawn)))
        return CommandResult(-1, "", "", True, "unknown", pending)
    except asyncio.CancelledError:
        spawn.add_done_callback(release_output)
        pending = _retain(asyncio.create_task(_settle_late_spawn(spawn)))
        result = CommandResult(-1, "", "", completion="unknown", settlement=pending)
        try:
            await asyncio.wait_for(asyncio.shield(pending), 5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass  # The retained settlement still owns the client.
        raise CommandCancelled(result.settled()) from None
    except Exception as exc:
        release_output()
        return CommandResult(1, "", _diagnostic(exc, "spawn"), completion="unknown")
    try:
        await asyncio.wait_for(process.wait(), timeout)
        if (current := asyncio.current_task()) is not None and current.cancelling():
            raise asyncio.CancelledError
        stdout, stderr = _read_output(output), _read_output(errors)
    except (asyncio.CancelledError, Exception) as exc:
        stopped = await asyncio.shield(_stop_client(process))
        diagnostic = "" if isinstance(exc, (asyncio.TimeoutError, asyncio.CancelledError)) else _diagnostic(exc, "communicate")
        result = CommandResult(-1, "", diagnostic, isinstance(exc, asyncio.TimeoutError),
                               "interrupted" if stopped else "unknown")
        if isinstance(exc, asyncio.CancelledError):
            raise CommandCancelled(result) from None
        return result
    finally:
        release_output()
    return CommandResult(int(process.returncode or 0), stdout, stderr)


def check_local_mode(args: Sequence[str]) -> dict:
    """Reject unverified producer modes before session effects, including config files."""
    from host_access import browser_launch
    browser_launch.inherited_args()
    forbidden = {"-p", "--provider", "--engine", "--cdp", "--auto-connect", "--config"}
    if sys.platform == "linux" and any(
            arg.split("=", 1)[0] in {"--args", "--executable-path"} for arg in args):
        raise identity.BrowserIdentityError(
            "Remove browser launch arguments from the command, then try again.")
    if any(arg.split("=", 1)[0] in forbidden for arg in args):
        raise identity.BrowserIdentityError(_LOCAL_MODE_REQUIRED)
    explicit = os.environ.get("AGENT_BROWSER_CONFIG")
    paths = [Path(explicit)] if explicit is not None else [Path.home() / ".agent-browser/config.json", Path.cwd() / "agent-browser.json"]
    config: dict = {}
    for path in paths:
        try:
            content = json.loads(path.read_text())
            if not isinstance(content, dict):
                raise ValueError
            config.update({key: value for key, value in content.items() if value is not None})
        except FileNotFoundError:
            if explicit is not None:
                raise identity.BrowserIdentityError(
                    "AGENT_BROWSER_CONFIG names a missing file; fix or unset it") from None
        except (OSError, ValueError) as exc:
            raise identity.BrowserIdentityError(
                "the agent-browser config file is not a readable JSON object; fix it") from exc
    if sys.platform == "linux" and (config.get("args") or config.get("executablePath")):
        raise identity.BrowserIdentityError(
            "Remove browser launch arguments from the agent-browser configuration, then try again.")
    provider = os.environ.get("AGENT_BROWSER_PROVIDER", config.get("provider"))
    engine = os.environ.get("AGENT_BROWSER_ENGINE", config.get("engine", "chrome"))
    auto_connect = os.environ.get("AGENT_BROWSER_AUTO_CONNECT", "").lower() in {"1", "true", "yes"}
    if (provider is not None or engine not in {None, "chrome"} or config.get("cdp")
            or os.environ.get("AGENT_BROWSER_CDP") or auto_connect or config.get("autoConnect")):
        raise identity.BrowserIdentityError(_LOCAL_MODE_REQUIRED)
    return config


_LOCAL_MODE_REQUIRED = (
    "managed browser requires the verified local Chrome/CDP mode; remove provider, engine, "
    "CDP, auto-connect and --config settings from the command, environment and agent-browser config"
)


async def close_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request_id: str) -> None:
    """Consume the local-CDP producer acknowledgement; transport EOF is not success."""
    try:
        writer.write((json.dumps({"id": request_id, "action": "close"}) + "\n").encode())
        await writer.drain()
        response = json_object((await reader.readline()).decode("utf-8"))
        data = response.get("data")
        if (response.get("id") != request_id or response.get("success") is not True
                or not isinstance(data, dict) or data.get("closed") is not True):
            raise identity.BrowserIdentityError("browser producer did not acknowledge close")
    finally:
        writer.close()
        await writer.wait_closed()
