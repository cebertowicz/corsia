"""Bounded, server-side MCP configuration discovery and Connected Apps import.

Detection exposes only app/source/name/kind/place metadata.  Selection accepts
only source/name pairs; configuration and credentials are reread locally and
never returned to the caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tomllib
from typing import Any

from connections.app_source_folder import clean_folder_label
from tools import mcp_secrets
from tools.mcp_config import MCPConfigError, allowed_names, http_options, timeout_seconds
from tools.mcp_import_reach import project_reach

_SOURCE_LABELS = {
    "claude_desktop": "Claude Desktop",
    "claude_code": "Claude Code",
    "cursor": "Cursor",
    "codex": "Codex",
}
log = logging.getLogger("corsia.key_audit")

# The import conversation is assembled from these canonical person-facing lines.
IMPORT_NONE_FOUND = "I didn't find any apps to bring over from Claude Code, Claude Desktop, Cursor or Codex."
IMPORT_UNSUPPORTED = "CORSIA can't connect to that kind of app yet."
IMPORT_FOLDER_WAITING_FOR_PLACE = "**{App}**: not working yet. Choose where it works, then approve it."
IMPORT_KEY_STAYS_IN_CODEX = "Its key stays in Codex, so you'll set it in Connected apps after it comes over."
IMPORT_NEEDS_SIGN_IN = "{App}: needs you to sign in. Press Sign in next to {App} in Settings › Connected apps."


def _home() -> Path:
    return Path.home()


def _paths(home: Path | None = None) -> dict[str, Path]:
    home = home or _home()
    desktop = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json" if os.name == "nt" else (
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    return {
        "claude_desktop": desktop,
        "claude_code": home / ".claude.json",
        "cursor": home / ".cursor" / "mcp.json",
        "codex": codex_home / "config.toml",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_servers(path: Path, key: str = "mcpServers") -> dict[str, dict[str, Any]]:
    value = _read_json(path).get(key)
    return {name: config for name, config in value.items()
            if isinstance(name, str) and isinstance(config, dict)} if isinstance(value, dict) else {}


def _kind(source: str, config: dict[str, Any]) -> str | None:
    command, url = config.get("command"), config.get("url")
    declared = config.get("type")
    if source == "claude_code":
        if declared == "sdk" or (isinstance(url, str) and not declared):
            return None
        if declared in {None, "stdio"} and isinstance(command, str) and command:
            return "stdio"
        if declared == "ws" and isinstance(url, str) and url:
            return "unsupported"
        if declared in {"http", "streamable-http", "sse"} and isinstance(url, str) and url:
            return "remote"
        return None
    if source == "claude_desktop":
        return "stdio" if isinstance(command, str) and command else None
    if source == "cursor":
        if isinstance(command, str) and command:
            return "stdio"
        return "remote" if isinstance(url, str) and url else None
    if source == "codex":
        if isinstance(command, str) and command:
            return "stdio"
        return "remote" if isinstance(url, str) and url else None
    return None


def _has_refused_proxy_authorization(config: dict[str, Any]) -> bool:
    """Treat proxy credentials as an unsupported import, never a token prompt."""
    headers = config.get("headers", config.get("http_headers"))
    return isinstance(headers, dict) and any(
        isinstance(name, str) and name.lower() == "proxy-authorization" for name in headers
    )


def _needs_sign_in(source: str, config: dict[str, Any]) -> bool:
    """Whether a remote app cannot be imported before C supplies sign-in."""
    if _kind(source, config) != "remote":
        return False
    if "oauth" in config:
        return True
    headers = config.get("headers", config.get("http_headers"))
    if isinstance(headers, dict):
        _clean, lifted = mcp_secrets.split_secrets("import", {"headers": headers}, headers=True)
        if lifted:
            return False
    if source == "codex" and ("bearer_token_env_var" in config or "env_http_headers" in config):
        return False
    # Accepted limitation: public remote apps needing no key wait for C, which
    # replaces this import-time decision with connect-time classification.
    return True


def _source_entries(source: str, path: Path) -> list[tuple[str, dict[str, Any], Path | None]]:
    """Read just supported MCP config subtrees; a folder is never exposed whole."""
    if not path.is_file():
        return []
    result: list[tuple[str, dict[str, Any], Path | None]] = []
    if source == "codex":
        servers = _read_toml(path).get("mcp_servers")
        if isinstance(servers, dict):
            for name, config in servers.items():
                if isinstance(name, str) and isinstance(config, dict):
                    result.append((name, config, None))
        return result
    if source != "claude_code":
        return [(name, config, None) for name, config in _json_servers(path).items()]

    document = _read_json(path)
    global_servers = document.get("mcpServers")
    if isinstance(global_servers, dict):
        for name, config in global_servers.items():
            if isinstance(name, str) and isinstance(config, dict):
                result.append((name, config, None))
    projects = document.get("projects")
    if not isinstance(projects, dict):
        return result
    for folder, project in projects.items():
        if not isinstance(folder, str) or not isinstance(project, dict):
            continue
        servers = project.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, config in servers.items():
            if isinstance(name, str) and isinstance(config, dict):
                result.append((name, config, Path(folder)))
    return result


def _source_servers(source: str, path: Path) -> dict[str, dict[str, Any]]:
    """Compatibility view for callers that need one deterministic config per name."""
    result: dict[str, dict[str, Any]] = {}
    for name, config, _folder in _source_entries(source, path):
        result.setdefault(name, config)
    return result


def _normalized_config(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def detected_sources(home: Path | None = None) -> list[dict[str, Any]]:
    """Return the safe, bounded source view used by the conductor."""
    results = []
    for source, path in _paths(home).items():
        servers: list[dict[str, str]] = []
        seen: set[str] = set()
        for name, config, folder in _source_entries(source, path):
            kind = _kind(source, config)
            if kind is None:
                continue
            digest = hashlib.sha256(_normalized_config(config).encode()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            item: dict[str, str] = {"name": name, "kind": kind}
            if folder is not None:
                # Path.name is the only persisted/displayed part of a project path.
                item["folder"] = clean_folder_label(folder.name or folder.resolve(strict=False).name) or ""
            servers.append(item)
        if servers:
            results.append({"source": source, "app": _SOURCE_LABELS[source], "servers": servers})
    return results


def _copyable_config(source: str, config: dict[str, Any]) -> dict[str, Any]:
    kind = _kind(source, config)
    if kind is None:
        raise MCPConfigError("app configuration is unsupported")
    if kind == "unsupported":
        raise MCPConfigError(IMPORT_UNSUPPORTED)
    copied: dict[str, Any] = {}
    if kind == "stdio":
        copied["command"] = config["command"]
        if "args" in config:
            copied["args"] = config["args"]
        if "env" in config:
            copied["env"] = config["env"]
    else:
        copied["url"] = config["url"]
        copied["transport"] = "sse" if config.get("type") == "sse" else "http"
        headers = config.get("headers", config.get("http_headers"))
        if headers is not None:
            copied["headers"] = headers
    if source == "codex" and ("bearer_token_env_var" in config or "env_http_headers" in config):
        copied["needs_token"] = True
        header_env = config.get("env_http_headers")
        if isinstance(header_env, dict):
            copied["secret_headers"] = [
                name for name, value in header_env.items()
                if isinstance(name, str) and name and isinstance(value, str) and value
            ]
    for source_key, target_key in (("connect_timeout", "connect_timeout"),
                                   ("startup_timeout_sec", "connect_timeout"),
                                   ("tool_timeout", "tool_timeout"),
                                   ("tool_timeout_sec", "tool_timeout")):
        if source_key in config:
            copied[target_key] = config[source_key]
    return copied


def _validate(server: str, config: dict[str, Any]) -> None:
    """Validate a config after credentials have been lifted into references."""
    clean, _ = mcp_secrets.split_secrets(server, config, headers=True)
    allowed_names(clean)
    timeout_seconds(clean, "connect_timeout", 10)
    timeout_seconds(clean, "tool_timeout", 30)
    if clean.get("url"):
        http_options(clean)
    elif not isinstance(clean.get("command"), str) or clean.get("bearer_file"):
        raise MCPConfigError("configure an HTTP URL or a stdio command")


def _selected_entry(source: str, name: str, home: Path | None) -> tuple[dict[str, Any], Path | None] | None:
    path = _paths(home).get(source)
    if path is None:
        return None
    for candidate_name, config, folder in _source_entries(source, path):
        if candidate_name == name:
            return config, folder
    return None


def prepare_import(selected: list[dict[str, str]], home: Path | None = None) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Select validated configs without returning any configuration values."""
    additions: dict[str, dict[str, Any]] = {}
    statuses: list[dict[str, str]] = []
    names = [item.get("name") for item in selected if isinstance(item, dict)]
    for item in selected:
        source, name = item.get("source"), item.get("name")
        if set(item) != {"source", "name"} or source not in _SOURCE_LABELS or not isinstance(name, str):
            statuses.append({"name": str(name or "unknown"), "status": "failed: select an app by source and name"})
            continue
        if names.count(name) > 1:
            statuses.append({"name": name, "status": "failed: selected app name conflicts with another selection"})
            continue
        entry = _selected_entry(source, name, home)
        if entry is None:
            statuses.append({"name": name, "status": "failed: app was not found in that client"})
            continue
        config, _folder = entry
        kind = _kind(source, config)
        try:
            copied = _copyable_config(source, config)
            _validate(name, copied)
        except MCPConfigError:
            statuses.append({"name": name, "kind": kind or "unknown", "status": "failed"})
            continue
        additions[name] = copied
        statuses.append({"name": name, "kind": kind or "unknown", "status": "pending"})
    return additions, statuses


def _key_refs(app_id: str, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    clean, lifted = mcp_secrets.split_secrets(app_id, config, headers=True)
    refs = [
        {"kind": kind, "name": name}
        for field, kind in (("secret_env", "env"), ("secret_headers", "header"))
        for name in clean.pop(field, [])
    ]
    return {**clean, "key_refs": refs}, lifted


def _sign_in_config(app_id: str, source: str, config: dict[str, Any]) -> dict[str, Any]:
    """Keep a hosted app's web settings while discarding its prior credentials."""
    copied = _copyable_config(source, config)
    copied.pop("needs_token", None)
    copied.pop("secret_headers", None)
    clean, _discarded = mcp_secrets.split_secrets(app_id, copied, headers=True)
    clean.pop("secret_env", None)
    clean.pop("secret_headers", None)
    return {**clean, "key_refs": []}


def audit_import_key_write(actor: str, source: str, name: str) -> None:
    """Audit import key writes without emitting the value or generated key name."""
    log.info("key import: actor=%s source=%s app=%s", actor, source, name)


def import_selected(
    selected: list[dict[str, str]], *, db, home: Path | None = None, actor: str = "unknown",
) -> dict[str, object]:
    """Create approval-gated Connected Apps from locally reread source/name pairs.

    This is deliberately the only import writer. It never reads or writes the
    retired ``tools.mcpServers`` setting.
    """
    from connections.app_lifecycle import create_app, create_pending_app
    from connections.app_reach import AppNameOverlapError
    from infra import key_store
    from infra.database import write_transaction
    import uuid

    if not isinstance(selected, list) or any(not isinstance(item, dict) for item in selected):
        raise MCPConfigError("select apps by source and name")
    rows: list[dict[str, str]] = []
    names = [item.get("name") for item in selected]
    for item in selected:
        source, name = item.get("source"), item.get("name")
        if set(item) != {"source", "name"} or source not in _SOURCE_LABELS or not isinstance(name, str):
            raise MCPConfigError("select apps by source and name")
        if names.count(name) > 1:
            rows.append({"name": name, "status": "failed"})
            continue
        entry = _selected_entry(source, name, home)
        if entry is None:
            rows.append({"name": name, "status": "failed"})
            continue
        source_config, folder = entry
        if _kind(source, source_config) == "unsupported" or _has_refused_proxy_authorization(source_config):
            rows.append({"name": name, "status": "cannot_connect"})
            continue
        reach, outside_folder = project_reach(folder, db=db)
        if reach is None:
            # The folder is display-only: it reaches nothing until the person chooses a place (G-P1).
            reach = {"mode": "selected", "project_ids": [], "outside_projects": False}
        try:
            app_id = str(uuid.uuid4())
            if _needs_sign_in(source, source_config):
                config = _sign_in_config(app_id, source, source_config)
                _validate(app_id, config)
                result = create_app(
                    name, config, reach, state="needs_sign_in", app_id=app_id,
                    created_by_thread_id=actor, audit_actor=("agent", actor), db=db,
                )
                rows.append({
                    "id": str(result["id"]), "name": name, "state": "needs_sign_in",
                    "message": IMPORT_NEEDS_SIGN_IN.format(App=name),
                })
                continue
            copied = _copyable_config(source, source_config)
            config, lifted = _key_refs(app_id, copied)
            _validate(app_id, {**config, "secret_env": [ref["name"] for ref in config["key_refs"] if ref["kind"] == "env"],
                               "secret_headers": [ref["name"] for ref in config["key_refs"] if ref["kind"] == "header"]})
            with write_transaction(db, scope="mcp_import"):
                result = create_pending_app(
                    name, config, reach, actor_thread_id=actor, app_id=app_id,
                    source_folder=outside_folder, audit_actor=("agent", actor), db=db,
                )
                for entry_name, value in lifted.items():
                    key_store.set_managed_value(entry_name, value, db=db)
                    audit_import_key_write(actor, _SOURCE_LABELS[source], name)
            row = {"id": str(result["id"]), "name": name, "state": "awaiting_approval"}
            if outside_folder is not None:
                row["folder"] = clean_folder_label(outside_folder) or ""
                row["message"] = IMPORT_FOLDER_WAITING_FOR_PLACE.format(App=name)
            rows.append(row)
        except AppNameOverlapError:
            rows.append({"name": name, "status": "already_exists"})
        except (MCPConfigError, key_store.KeyStoreError):
            rows.append({"name": name, "status": "failed"})
    return {"rows": rows}


def save_import(_additions: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Refuse the retired settings-backed writer; import_selected is canonical."""
    raise MCPConfigError("add the app in Connected apps")
