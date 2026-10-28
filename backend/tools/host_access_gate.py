"""One bounded admission seam for reviewed temporary host actions.

The model never receives a lease or permit identifier.  The registry obtains an
opaque authorization, installs its private permit in a task-local ``ContextVar``
only around the concrete tool call, and lower effect layers atomically consume
that permit after recomputing the exact action.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Iterator, Mapping
# Browser navigation validates its own web-only URLs.
from uuid import uuid4

from loguru import logger

from host_access.redaction import CommandRedactionError, redact_shell_command
from host_access.refusal_messages import (
    LITERAL_CREDENTIAL_REFUSAL,
    descriptor_refusal,
    normalization_refusal,
    protected_scope_denial,
    reviewer_refusal,
)
from host_access.credential_sanitizer import sanitize_credential_data
from host_access.supervisor import (
    consume_host_action_permit as _consume_host_action_permit,
)
from host_access.reviewer_unavailable import action_review_refusal
from host_access.supervisor import ActionReviewUnavailableError
from host_access.supervisor import HostAccessSupervisorError
from host_access.supervisor import ProjectPolicyChangedError
from host_access.supervisor import failure_detail
from host_access.supervisor import review_host_action as _review_host_action
from host_access.supervisor import (
    resolve_active_host_access_lease as _resolve_owned_lease,
)
from infra.db_async import run_in_db
from tools.host_access_errors import HostAccessBlocked, HostAccessGateError, HostAccessRequired
from tools.host_access_external import (
    ExternalLaunchMismatch,
    classify_external_launch,
)
from tools.sandbox_config import check_repo_write_path, is_path_allowed


_ACTION_DESCRIPTOR_VERSION = 1
_RESERVED_PREFIXES = ("_session_", "_host_access_")
_PATH_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "multi_edit",
        "list_dir",
        "glob",
        "grep",
        "browser_screenshot",
    }
)
_HOST_ADAPTERS = _PATH_TOOLS | {
    "bash",
    "browser_open",
    "process_discover",
    "process_signal",
}


@dataclass(frozen=True, slots=True)
class _PreparedAction:
    tool_name: str
    descriptor_json: str = field(repr=False)
    action_hash: str
    requires_elevation: bool
    lexical_path: Path | None = None
    resolved_path: Path | None = None
    host_scope: bool = False
    working_directory: Path | None = None
    working_directory_device: int | None = field(default=None, repr=False)
    working_directory_inode: int | None = field(default=None, repr=False)
    effective_timeout: int | None = None
    parameter_overrides: tuple[tuple[str, Any], ...] = ()
    literal_command: str | None = field(default=None, repr=False)
    literal_credentials: int = field(default=0, repr=False)

    def review_descriptor(self) -> dict[str, Any]:
        value = json.loads(self.descriptor_json)
        if not isinstance(value, dict):  # pragma: no cover - construction invariant
            raise HostAccessBlocked("Host action descriptor is invalid.")
        return value


@dataclass(frozen=True, slots=True)
class _ActionPermit:
    permit_id: str = field(repr=False)
    lease_id: str = field(repr=False)
    thread_id: str
    lease_revision: int
    application_hash: str = field(repr=False)
    action: _PreparedAction
    external_agent: bool = field(default=False, repr=False)


@dataclass(frozen=True, slots=True)
class HostActionAuthorization:
    """Opaque registry result; only safe parameter overrides are public."""

    _permit: _ActionPermit | None = field(default=None, repr=False)
    _parameter_overrides: tuple[tuple[str, Any], ...] = field(
        default=(),
        repr=False,
    )

    @property
    def parameter_overrides(self) -> dict[str, Any]:
        return dict(self._parameter_overrides)


@dataclass(frozen=True, slots=True)
class ConsumedHostAction:
    """Permit-free exact values the lower layer may now use for its effect."""

    tool_name: str
    resolved_path: Path | None = None
    working_directory: Path | None = None
    effective_timeout: int | None = None
    _working_directory_fd: int | None = field(default=None, repr=False)
    lease_id: str | None = field(default=None, repr=False)
    lease_revision: int | None = field(default=None, repr=False)
    permit_id: str | None = field(default=None, repr=False)
    external_agent: bool = field(default=False, repr=False)


_current_action_permit: ContextVar[_ActionPermit | None] = ContextVar(
    "host_access_action_permit",
    default=None,
)


def _canonical_tool_name(name: str | None) -> str:
    # Import lazily: tools.catalog imports ToolRegistry for surface generation.
    from tools.catalog import normalize_tool_name

    return normalize_tool_name(name)


def _safe_params(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in dict(params).items()
        if not str(key).startswith(_RESERVED_PREFIXES)
    }


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    return value


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise HostAccessBlocked("Host action parameters are invalid.")
    return value


def _integer(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostAccessBlocked("Host action parameters are invalid.")
    if value < minimum:
        raise HostAccessBlocked("Host action parameters are invalid.")
    return min(value, maximum)


def _number(value: Any, field_name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum:
        raise HostAccessBlocked(f"Host action {field_name} is invalid.")
    return normalized


def _tool_workspace(tool: Any) -> Path:
    configured = getattr(tool, "_workspace", None)
    if configured is not None:
        return Path(configured).expanduser()
    from infra.config import WORKSPACE

    return WORKSPACE


def _absolute_path(value: Any, *, base: Path) -> tuple[Path, Path]:
    raw = Path(_text(value, "path")).expanduser()
    if not raw.is_absolute():
        raw = base / raw
    lexical = Path(os.path.abspath(str(raw)))
    return lexical, lexical.resolve(strict=False)


def _path_payload(lexical: Path, resolved: Path) -> dict[str, str]:
    return {"lexical": str(lexical), "resolved": str(resolved)}


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise HostAccessBlocked(
            "Host action working directory is unavailable."
        ) from exc
    if not stat.S_ISDIR(status.st_mode):
        raise HostAccessBlocked("Host action working directory is invalid.")
    return int(status.st_dev), int(status.st_ino)


def _sha256_text(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8")
    return {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _make_prepared(
    tool_name: str,
    payload: dict[str, Any],
    *,
    requires_elevation: bool,
    lexical_path: Path | None = None,
    resolved_path: Path | None = None,
    working_directory: Path | None = None,
    working_directory_device: int | None = None,
    working_directory_inode: int | None = None,
    effective_timeout: int | None = None,
    parameter_overrides: Mapping[str, Any] | None = None,
    binding_payload: Mapping[str, Any] | None = None,
    host_scope: bool = False,
    literal_command: str | None = None,
    literal_credentials: int = 0,
) -> _PreparedAction:
    review_payload = (
        payload if tool_name == "bash" else sanitize_credential_data(payload)
    )
    descriptor = {
        "version": _ACTION_DESCRIPTOR_VERSION,
        "tool": tool_name,
        **review_payload,
    }
    binding_descriptor = {
        "version": _ACTION_DESCRIPTOR_VERSION,
        "tool": tool_name,
        **(binding_payload if binding_payload is not None else payload),
    }
    try:
        encoded = json.dumps(
            descriptor,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        binding_encoded = json.dumps(
            binding_descriptor,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        binding_hash = hashlib.sha256(binding_encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HostAccessBlocked(descriptor_refusal(exc)) from exc
    return _PreparedAction(
        tool_name=tool_name,
        descriptor_json=encoded,
        action_hash=binding_hash,
        requires_elevation=requires_elevation,
        lexical_path=lexical_path,
        resolved_path=resolved_path,
        host_scope=host_scope,
        working_directory=working_directory,
        working_directory_device=working_directory_device,
        working_directory_inode=working_directory_inode,
        effective_timeout=effective_timeout,
        parameter_overrides=tuple((parameter_overrides or {}).items()),
        literal_command=literal_command,
        literal_credentials=literal_credentials,
    )


def _path_requires_elevation(
    *,
    lexical: Path,
    resolved: Path,
    for_write: bool,
) -> bool:
    from tools.host_path_policy import path_review_classification

    # Every path-bearing host action is prepared here, when a permit is
    # requested and again before it is consumed: a hard-floor path is refused
    # before any review happens or any permit is honoured.
    ordinary_denied, hard_floor = path_review_classification(
        lexical, resolved, for_write=for_write
    )
    if hard_floor:
        raise HostAccessBlocked(hard_floor)
    if for_write:
        try:
            protected_error = check_repo_write_path(resolved)
        except Exception as exc:
            raise HostAccessBlocked(
                "Repository write protection is unavailable."
            ) from exc
        ordinary_denied = ordinary_denied or bool(protected_error)
    try:
        allowed = is_path_allowed(lexical, for_write=for_write)
    except Exception as exc:
        raise HostAccessBlocked("Host path policy is unavailable.") from exc
    return ordinary_denied or not allowed


def _path_action(
    canonical: str,
    params: Mapping[str, Any],
    *,
    tool: Any,
) -> _PreparedAction:
    base = _tool_workspace(tool)
    raw_path = params.get("path")
    if canonical in {"glob", "grep"} and not raw_path:
        raw_path = "."
    lexical, resolved = _absolute_path(raw_path, base=base)
    for_write = canonical in {
        "write_file",
        "edit_file",
        "multi_edit",
        "browser_screenshot",
    }
    requires_elevation = _path_requires_elevation(
        lexical=lexical,
        resolved=resolved,
        for_write=for_write,
    )
    payload: dict[str, Any] = {"path": _path_payload(lexical, resolved)}

    if canonical == "read_file":
        payload["offset"] = params.get("offset")
        payload["limit"] = params.get("limit")
    elif canonical == "write_file":
        payload["content"] = _sha256_text(_text(params.get("content"), "content"))
    elif canonical == "edit_file":
        payload["old_string"] = _sha256_text(
            _text(params.get("old_string"), "old_string")
        )
        payload["new_string"] = _sha256_text(
            _text(params.get("new_string"), "new_string")
        )
        payload["replace_all"] = _boolean(params.get("replace_all"))
    elif canonical == "multi_edit":
        edits = params.get("edits")
        if not isinstance(edits, list) or not edits:
            raise HostAccessBlocked("Host action edits are invalid.")
        payload["edits"] = [
            {
                "old_string": _sha256_text(_text(edit.get("old_string"), "old_string")),
                "new_string": _sha256_text(_text(edit.get("new_string"), "new_string")),
                "replace_all": _boolean(edit.get("replace_all")),
            }
            for edit in edits
            if isinstance(edit, Mapping)
        ]
        if len(payload["edits"]) != len(edits):
            raise HostAccessBlocked("Host action edits are invalid.")
    elif canonical == "glob":
        payload.update(
            {
                "pattern": _text(params.get("pattern"), "pattern"),
                "all_threads": _boolean(params.get("all_threads")),
            }
        )
    elif canonical == "grep":
        payload.update(
            {
                "pattern": _text(params.get("pattern"), "pattern"),
                "glob": params.get("glob"),
                "output_mode": params.get("output_mode") or "files",
                "case_insensitive": _boolean(params.get("case_insensitive")),
                "context": _integer(
                    params.get("context"),
                    0,
                    minimum=0,
                    maximum=10,
                ),
                "max_results": _integer(
                    params.get("max_results"),
                    50,
                    minimum=1,
                    maximum=200,
                ),
                "all_threads": _boolean(params.get("all_threads")),
            }
        )
    elif canonical == "browser_screenshot":
        payload["full_page"] = _boolean(params.get("full_page"))

    return _make_prepared(
        canonical,
        payload,
        requires_elevation=requires_elevation,
        lexical_path=lexical,
        resolved_path=resolved,
    )


def _bash_action(params: Mapping[str, Any], *, tool: Any) -> _PreparedAction:
    command = _text(params.get("command"), "command")
    # Credentials are normalized so the reviewer can assess command semantics
    # without receiving or durably recording their values. Literal credential
    # ingress remains rejected by ``authorize_host_action`` below.
    try:
        redacted_command = redact_shell_command(command)
    except CommandRedactionError as exc:
        raise HostAccessBlocked(normalization_refusal(exc)) from exc
    raw_cwd = (
        params.get("working_dir") or getattr(tool, "working_dir", None) or os.getcwd()
    )
    lexical_cwd, resolved_cwd = _absolute_path(raw_cwd, base=Path.cwd())
    cwd_device, cwd_inode = _directory_identity(resolved_cwd)
    timeout = _integer(
        params.get("timeout"),
        int(getattr(tool, "default_timeout", 120)),
        minimum=1,
        maximum=600,
    )
    background = _boolean(params.get("background"))
    host_scope = _boolean(params.get("host"))
    common_payload = {
        "working_directory": str(resolved_cwd),
        "timeout_seconds": timeout,
        "background": background,
    }
    binding_payload: dict[str, Any] = {
        "command": command,
        **common_payload,
        "working_directory_identity": {
            "device": cwd_device,
            "inode": cwd_inode,
        },
    }
    if host_scope:
        # Host and long-running modes bind into the action hash, not the fixed
        # reviewer descriptor, so a permit for one envelope cannot replay for
        # another. The reviewer only sees host:true bash.
        binding_payload["host"] = True
    if _boolean(params.get("long_running")):
        binding_payload["long_running"] = True
    return _make_prepared(
        "bash",
        {
            "command": redacted_command.descriptor(),
            **common_payload,
        },
        binding_payload=binding_payload,
        requires_elevation=True,
        host_scope=host_scope,
        working_directory=resolved_cwd,
        working_directory_device=cwd_device,
        working_directory_inode=cwd_inode,
        effective_timeout=timeout,
        literal_command=command,
        literal_credentials=redacted_command.literal_credentials,
    )


def _process_action(
    canonical: str,
    params: Mapping[str, Any],
) -> _PreparedAction:
    if canonical == "process_discover":
        raw_pid = params.get("pid")
        pid = (
            None
            if raw_pid is None
            else _integer(raw_pid, 0, minimum=2, maximum=2_147_483_647)
        )
        raw_query = params.get("query")
        if raw_query is not None and (
            not isinstance(raw_query, str) or len(raw_query) > 200
        ):
            raise HostAccessBlocked("Host action query is invalid.")
        return _make_prepared(
            canonical,
            {
                "pid": pid,
                "query": raw_query,
                "limit": _integer(
                    params.get("limit"),
                    20,
                    minimum=1,
                    maximum=50,
                ),
            },
            requires_elevation=True,
        )

    pid = _integer(
        params.get("pid"),
        0,
        minimum=2,
        maximum=2_147_483_647,
    )
    create_time = _number(
        params.get("create_time"),
        "create_time",
        minimum=0.0,
    )
    executable_fingerprint = _text(
        params.get("executable_fingerprint"),
        "executable_fingerprint",
    )
    if len(executable_fingerprint) != 64 or any(
        char not in "0123456789abcdef" for char in executable_fingerprint
    ):
        raise HostAccessBlocked("Host action executable_fingerprint is invalid.")
    selected_signal = params.get("signal")
    if selected_signal not in {"interrupt", "terminate", "kill"}:
        raise HostAccessBlocked("Host action signal is invalid.")
    from host_access.processes import (
        ProcessBindingError,
        describe_bound_process,
    )

    try:
        target = describe_bound_process(
            pid=pid,
            create_time=create_time,
            executable_fingerprint=executable_fingerprint,
        )
    except ProcessBindingError as exc:
        raise HostAccessBlocked("Host action process binding is unavailable.") from exc
    return _make_prepared(
        canonical,
        {
            "pid": pid,
            "create_time": create_time,
            "executable_fingerprint": executable_fingerprint,
            "signal": selected_signal,
            "target": {
                "pid": target.pid,
                "parent_pid": target.parent_pid,
                "name": target.name,
                "executable": target.executable,
                "arguments": list(target.arguments),
                "create_time": target.create_time,
                "executable_fingerprint": target.executable_fingerprint,
            },
        },
        requires_elevation=True,
    )



def _prepare_action(
    tool_name: str,
    params: Mapping[str, Any],
    *,
    tool: Any,
) -> _PreparedAction | None:
    canonical = _canonical_tool_name(tool_name)
    if canonical not in _HOST_ADAPTERS:
        return None
    safe = _safe_params(params)
    if canonical == "bash":
        return _bash_action(safe, tool=tool)
    if canonical == "browser_open":
        return None
    if canonical in {"process_discover", "process_signal"}:
        return _process_action(canonical, safe)
    return _path_action(canonical, safe, tool=tool)


async def _resolve_active_lease(thread_id: str) -> dict[str, Any] | None:
    try:
        return await _resolve_owned_lease(thread_id)
    except Exception as exc:
        # The model-visible message stays generic: lease internals must not
        # leak. The full cause goes to the server log for diagnosis.
        logger.exception(
            "host-access lease resolution crashed for session={}", thread_id
        )
        raise HostAccessBlocked("Host-access lineage could not be verified.") from exc


async def _workspace_write_requires_review(
    *,
    action: _PreparedAction,
    thread_id: str,
    project_id: str | None,
    params: Mapping[str, Any],
) -> bool:
    if action.resolved_path is None:
        raise HostAccessBlocked("Host action path is unavailable.")
    # Durable-write governance resolves scope against the canonical selected
    # workspace root — deliberately not the tool workspace, which is only the
    # base directory for relative tool paths. For harness sessions the tool
    # workspace is the session working directory (often a project directory)
    # and must never redefine workspace identity for policy resolution (#830).
    from infra.config import WORKSPACE
    from registry.shared_documents import workspace_write_requires_review
    from registry.shared_write_errors import DocumentOwnershipRefused
    try:
        return await run_in_db(
            lambda: workspace_write_requires_review(
                action.resolved_path,
                thread_id=thread_id,
                project_id=project_id,
                workspace=WORKSPACE,
                description=params.get("description"),
                **{key: params.get(key) for key in ("owner", "update_trigger")},
            )
        )
    except DocumentOwnershipRefused as refusal:
        # Final for every agent, lease or not: never routed to the host reviewer.
        raise HostAccessBlocked(str(refusal)) from refusal
    except Exception as exc:
        logger.exception(
            "workspace write-policy check crashed for path={} session={}",
            action.resolved_path,
            thread_id,
        )
        raise HostAccessBlocked(
            "Workspace write-policy check failed internally for "
            f"{action.resolved_path} (an internal fault, not a policy "
            f"denial): {failure_detail(exc)}"
        ) from exc


def _new_screenshot_name() -> str:
    return f"browser-{uuid4().hex}.png"


async def _resolve_thread_root(
    thread_id: str,
    project_id: str | None,
) -> Path:
    def resolve() -> Path:
        from core.skills_registry import skill_scope_root
        from infra.config import WORKSPACE
        from infra.database import get_db

        db = get_db()
        try:
            root = skill_scope_root(
                scope="thread",
                workspace=WORKSPACE,
                db=db,
                project_id=project_id,
                thread_id=thread_id,
            )
        finally:
            db.close()
        if root is None:
            raise HostAccessBlocked(
                "Thread screenshot destination could not be resolved."
            )
        return root

    try:
        return await run_in_db(resolve)
    except HostAccessGateError:
        raise
    except Exception as exc:
        raise HostAccessBlocked(
            "Thread screenshot destination could not be resolved."
        ) from exc


async def _materialize_defaults(
    *,
    thread_id: str,
    project_id: str | None,
    tool_name: str,
    params: Mapping[str, Any],
    tool: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    safe = _safe_params(params)
    overrides: dict[str, Any] = {}
    if _canonical_tool_name(tool_name) == "browser_screenshot" and not safe.get("path"):
        thread_root = await _resolve_thread_root(thread_id, project_id)
        path = thread_root / "working" / "screenshots" / _new_screenshot_name()
        safe["path"] = str(path)
        overrides["path"] = str(path)
    if _canonical_tool_name(tool_name) == "write_file":
        raw_path = safe.get("path")
        if isinstance(raw_path, str) and Path(raw_path).name == raw_path:
            current_candidate = _tool_workspace(tool) / raw_path
            if not current_candidate.exists():
                thread_root = await _resolve_thread_root(thread_id, project_id)
                path = thread_root / "working" / raw_path
                safe["path"] = str(path)
                overrides["path"] = str(path)
    return safe, overrides


def _permit_from_review(
    *,
    lease: Mapping[str, Any],
    thread_id: str,
    action: _PreparedAction,
    result: Mapping[str, Any],
    external_agent: bool,
) -> _ActionPermit:
    review = result.get("review")
    returned_lease = result.get("lease")
    if not isinstance(review, Mapping) or not isinstance(returned_lease, Mapping):
        raise HostAccessBlocked("Host action review returned an invalid result.")
    if review.get("verdict") != "allow":
        raise HostAccessBlocked(reviewer_refusal(review))
    permit_id = review.get("permit_id")
    expected = {
        "lease_id": lease.get("id"),
        "lease_revision": lease.get("revision"),
        "thread_id": thread_id,
        "tool_name": action.tool_name,
        "action_hash": action.action_hash,
    }
    if (
        not isinstance(permit_id, str)
        or not permit_id
        or any(review.get(key) != value for key, value in expected.items())
        or returned_lease.get("id") != lease.get("id")
        or returned_lease.get("revision") != lease.get("revision")
        or returned_lease.get("application_hash") != lease.get("application_hash")
        or returned_lease.get("state") != "active"
    ):
        raise HostAccessBlocked("Host action review binding is invalid.")
    lease_id = lease.get("id")
    application_hash = lease.get("application_hash")
    revision = lease.get("revision")
    if (
        not isinstance(lease_id, str)
        or not isinstance(application_hash, str)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
    ):
        raise HostAccessBlocked("Host action lease binding is invalid.")
    return _ActionPermit(
        permit_id=permit_id,
        lease_id=lease_id,
        thread_id=thread_id,
        lease_revision=revision,
        application_hash=application_hash,
        action=action,
        external_agent=external_agent,
    )


async def _review_action(
    lease: Mapping[str, Any],
    *,
    thread_id: str,
    action: _PreparedAction,
) -> Mapping[str, Any]:
    """Run an action review and expose only bounded refusal guidance."""
    try:
        return await _review_host_action(
            str(lease["id"]),
            thread_id=thread_id,
            tool_name=action.tool_name,
            action_hash=action.action_hash,
            redacted_descriptor=action.review_descriptor(),
        )
    except ProjectPolicyChangedError as exc:
        raise HostAccessBlocked("Project policy changed; request host access again.") from exc
    except ActionReviewUnavailableError as exc:
        raise HostAccessBlocked(action_review_refusal(exc)) from exc
    except HostAccessGateError:
        raise
    except HostAccessSupervisorError as exc:
        # Supervisor messages are bounded, self-descriptive, and secret-free;
        # surface them verbatim so the holder learns which check refused.
        raise HostAccessBlocked(str(exc)) from exc
    except Exception as exc:
        # Unexpected exception text is unbounded and may embed paths or values;
        # surface only the bounded single-line sanitized form.
        raise HostAccessBlocked(
            f"Host action review failed closed: {failure_detail(exc)}"
        ) from exc


async def authorize_host_action(
    *,
    thread_id: str,
    project_id: str | None,
    tool: Any,
    tool_name: str,
    params: Mapping[str, Any],
) -> HostActionAuthorization:
    """Classify one call and obtain a fresh private permit when required."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise HostAccessBlocked("Exact tool-call identity is unavailable.")
    materialized, overrides = await _materialize_defaults(
        thread_id=thread_id,
        project_id=project_id,
        tool_name=tool_name,
        params=params,
        tool=tool,
    )
    action = await asyncio.to_thread(_prepare_action, tool_name, materialized, tool=tool)
    if action is None:
        return HostActionAuthorization(_parameter_overrides=tuple(overrides.items()))
    if action.parameter_overrides:
        overrides.update(dict(action.parameter_overrides))
    if action.tool_name == "bash" and action.literal_credentials:
        raise HostAccessBlocked(LITERAL_CREDENTIAL_REFUSAL)
    ordinary_action = action.tool_name != "bash" and not action.requires_elevation
    if ordinary_action:
        # Workspace organization is stricter than the host path allowlist.
        # Classify that one lower-layer denial before deciding lease admission.
        review_workspace_write = action.tool_name in {
            "write_file",
            "edit_file",
            "multi_edit",
        } and await _workspace_write_requires_review(
            action=action,
            thread_id=thread_id,
            project_id=project_id,
            params=materialized,
        )
        if not review_workspace_write:
            return HostActionAuthorization(
                _parameter_overrides=tuple(overrides.items())
            )
    # A default (host:false) bash call always runs in the protected envelope —
    # same jail, same guards, no review, zero tokens — regardless of whether a
    # lease happens to be active. Only host:true opts a single call into review.
    if action.tool_name == "bash" and not action.host_scope:
        return HostActionAuthorization(
            _parameter_overrides=tuple(overrides.items())
        )
    lease = await _resolve_active_lease(thread_id)
    if lease is None:
        if ordinary_action:
            return HostActionAuthorization(
                _parameter_overrides=tuple(overrides.items())
            )
        if action.tool_name == "bash":
            # host:true with no active lease is a fast structured denial, never
            # a wall — host scope is only reviewable under an already-active
            # lease, so the guidance sends the agent to request one first.
            raise HostAccessRequired(
                protected_scope_denial(
                    "host scope was requested but no host-access lease is "
                    "active",
                    lease_active=False,
                )
            )
        raise HostAccessRequired(
            protected_scope_denial(
                "this action is outside protected scope",
                lease_active=False,
            )
        )
    try:
        external_agent = classify_external_launch(
            lease,
            host_bash=action.tool_name == "bash" and action.host_scope,
            command=action.literal_command,
            working_directory=action.working_directory,
        )
    except ExternalLaunchMismatch as exc:
        raise HostAccessBlocked(str(exc)) from exc
    result = await _review_action(lease, thread_id=thread_id, action=action)
    permit = _permit_from_review(
        lease=lease,
        thread_id=thread_id,
        action=action,
        result=result,
        external_agent=external_agent,
    )
    return HostActionAuthorization(
        _permit=permit,
        _parameter_overrides=tuple(overrides.items()),
    )


@contextmanager
def host_action_permit_scope(
    authorization: HostActionAuthorization,
) -> Iterator[None]:
    """Install exactly one private permit for the concrete tool call."""
    if not isinstance(authorization, HostActionAuthorization):
        raise HostAccessBlocked("Host action authorization is invalid.")
    token: Token[_ActionPermit | None] = _current_action_permit.set(
        authorization._permit
    )
    try:
        yield
    finally:
        _current_action_permit.reset(token)


async def consume_current_action_permit(
    *,
    thread_id: str | None,
    project_id: str | None,
    tool: Any,
    tool_name: str,
    params: Mapping[str, Any],
) -> ConsumedHostAction | None:
    """Recompute and atomically consume the current permit before an effect."""
    permit = _current_action_permit.get()
    if permit is None:
        return None
    if thread_id != permit.thread_id:
        raise HostAccessBlocked("Host action identity changed before execution.")
    materialized, _overrides = await _materialize_defaults(
        thread_id=permit.thread_id,
        project_id=project_id,
        tool_name=tool_name,
        params=params, tool=tool,
    )
    action = await asyncio.to_thread(_prepare_action, tool_name, materialized, tool=tool)
    if (
        action is None
        or action.tool_name != permit.action.tool_name
        or action.action_hash != permit.action.action_hash
    ):
        raise HostAccessBlocked("Host action changed after safety review.")
    working_directory_fd: int | None = None
    if action.tool_name == "bash":
        expected_device = permit.action.working_directory_device
        expected_inode = permit.action.working_directory_inode
        if (
            action.working_directory is None
            or expected_device is None
            or expected_inode is None
        ):
            raise HostAccessBlocked(
                "Host action working directory binding is unavailable."
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            working_directory_fd = os.open(action.working_directory, flags)
            opened = os.fstat(working_directory_fd)
        except OSError as exc:
            if working_directory_fd is not None:
                os.close(working_directory_fd)
            raise HostAccessBlocked(
                "Host action working directory changed after safety review."
            ) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or int(opened.st_dev) != expected_device
            or int(opened.st_ino) != expected_inode
        ):
            os.close(working_directory_fd)
            raise HostAccessBlocked(
                "Host action working directory changed after safety review."
            )
    try:
        await _consume_host_action_permit(
            permit.permit_id,
            lease_id=permit.lease_id,
            thread_id=permit.thread_id,
            expected_revision=permit.lease_revision,
            application_hash=permit.application_hash,
            tool_name=action.tool_name,
            action_hash=action.action_hash,
        )
    except ProjectPolicyChangedError as exc:
        if working_directory_fd is not None:
            os.close(working_directory_fd)
        raise HostAccessBlocked(
            "Project policy changed; request host access again."
        ) from exc
    except HostAccessGateError:
        if working_directory_fd is not None:
            os.close(working_directory_fd)
        raise
    except Exception as exc:
        if working_directory_fd is not None:
            os.close(working_directory_fd)
        raise HostAccessBlocked("Host action permit could not be consumed.") from exc
    except BaseException:
        if working_directory_fd is not None:
            os.close(working_directory_fd)
        raise
    return ConsumedHostAction(
        tool_name=action.tool_name,
        resolved_path=action.resolved_path,
        working_directory=action.working_directory,
        effective_timeout=action.effective_timeout,
        _working_directory_fd=working_directory_fd,
        lease_id=permit.lease_id,
        lease_revision=permit.lease_revision,
        permit_id=permit.permit_id,
        external_agent=permit.external_agent,
    )
