"""Server-side authentication and validation for operator-configured MCP."""

import os
from contextvars import ContextVar
import logging
from pathlib import Path
import re
import stat
import traceback
from urllib.parse import urlsplit

import httpx


class MCPConfigError(ValueError):
    """An actionable configuration error whose message contains no input values."""


def read_bearer_file(path: str) -> str:
    """Read a small, private regular file without following a final symlink."""
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) not in (0o600, 0o400)):
                raise MCPConfigError("bearer_file must be owned by this user and have mode 0600 or 0400")
            raw = stream.read(8193)
        value = raw.decode("utf-8").rstrip("\r\n")
        if len(raw) > 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value):
            raise MCPConfigError("bearer_file must contain one nonempty bearer token")
        return value
    except (OSError, UnicodeError, TypeError):
        raise MCPConfigError("bearer_file is unreadable; use a private regular file, not a symlink") from None


class BearerFileAuth(httpx.Auth):
    """Re-read the file per request; retain only recent values for redaction."""

    def __init__(self, path: str):
        self._path = path
        self._tokens = [read_bearer_file(path)]

    def auth_flow(self, request):
        token = read_bearer_file(self._path)
        if token != self._tokens[-1]:
            self._tokens = [self._tokens[-1], token]
        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    def redact(self, value):
        """Remove credential echoes from remote schemas and results."""
        if isinstance(value, str):
            for token in self._tokens:
                value = value.replace(token, "[REDACTED]")
        elif isinstance(value, dict):
            value = {self.redact(key): self.redact(item) for key, item in value.items()}
        elif isinstance(value, list):
            value = [self.redact(item) for item in value]
        return value


_active_auth: ContextVar[BearerFileAuth | None] = ContextVar("mcp_auth", default=None)


class _TransportLogFilter(logging.Filter):
    def filter(self, record):
        auth = _active_auth.get()
        if auth is not None:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + "".join(traceback.format_exception(*record.exc_info))
                record.exc_info = record.exc_text = None
            record.msg, record.args = auth.redact(message), ()
        return True


_log_filter = _TransportLogFilter()


def protect_transport_logs(auth: BearerFileAuth | None) -> None:
    """Redact SDK logs in this connection task and its inherited SDK tasks."""
    _active_auth.set(auth)
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(("mcp.", "httpcore.", "httpx")):
            logging.getLogger(name).addFilter(_log_filter)


def timeout_seconds(config: dict, field: str, default: float) -> float:
    """Reject unbounded, nonnumeric or excessive timeout configuration."""
    value = config.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 < value <= 300:
        raise MCPConfigError(f"{field} must be a number greater than zero and at most 300 seconds")
    return value


def allowed_names(config: dict) -> set[str] | None:
    """Resolve exact upstream names, preserving unrestricted legacy configs."""
    names = config.get("allowed_tools", config.get("tool_allowlist"))
    if "allowed_tools" in config and "tool_allowlist" in config:
        if config["allowed_tools"] != config["tool_allowlist"]:
            raise MCPConfigError("allowed_tools and tool_allowlist disagree")
    if "allowed_tools" not in config and "tool_allowlist" not in config:
        return None
    if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
        raise MCPConfigError("allowed_tools must be a list of exact upstream tool names")
    return set(names)


def public_name(server_name: str, original: str, config: dict) -> str:
    """Map one upstream tool to a stable, provider-compatible public name."""
    mapping = config.get("public_names", {})
    prefix = config.get("tool_prefix", f"mcp_{server_name}_")
    if not isinstance(mapping, dict) or not isinstance(prefix, str):
        raise MCPConfigError("public_names must be an object and tool_prefix must be a string")
    name = mapping.get(original, prefix + original)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise MCPConfigError("public tool names must contain 1–64 letters, digits, underscores or hyphens")
    return name


def http_options(config: dict) -> tuple[dict, BearerFileAuth | None]:
    """Build private client options, refusing credentials in URLs or redirects."""
    url = urlsplit(config["url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise MCPConfigError("MCP URL must be HTTP(S) without credentials, query or fragment")
    from host_access.credential_sanitizer import sanitize_credential_data

    headers = config.get("headers") or {}
    if (not isinstance(headers, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items())):
        raise MCPConfigError("headers must be an object of non-secret string values")
    if (any(key.lower() in {"authorization", "proxy-authorization", "cookie"} for key in headers)
            or sanitize_credential_data(headers) != headers):
        raise MCPConfigError("literal credential headers are refused; use bearer_file for bearer authentication")
    auth = BearerFileAuth(config["bearer_file"]) if config.get("bearer_file") else None
    return {"auth": auth, "headers": headers, "follow_redirects": not bool(auth),
            "timeout": httpx.Timeout(10, read=timeout_seconds(config, "tool_timeout", 30))}, auth


def safe_error(error: BaseException) -> str:
    """Classify transport failures without rendering exceptions or server bodies."""
    if isinstance(error, BaseExceptionGroup):
        return safe_error(error.exceptions[0])
    if isinstance(error, MCPConfigError):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code == 401:
            return "HTTP 401: authentication rejected; check the server's bearer_file"
        return f"HTTP {error.response.status_code}: server rejected the request"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "request timed out; check server availability"
    return "server unavailable or protocol failed; check the MCP service and configuration"
