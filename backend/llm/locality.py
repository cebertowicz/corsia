"""Canonical locality facts for catalog rows and project cloud policy."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse


def endpoint_on_this_mac(api_base) -> bool:
    """Return whether an API base resolves to this Mac's loopback endpoint."""
    host = urlparse(str(api_base or "")).hostname or ""
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _server_id(row: dict) -> str | None:
    """Return the network server identity encoded by one catalog row."""
    selector = str(row.get("litellm_model") or row.get("selector") or "")
    prefix, separator, remainder = selector.partition("@")
    if not separator or prefix not in {"omlx", "external"}:
        return None
    server_id, slash, _model = remainder.partition("/")
    return server_id if slash and server_id else None


def server_on_this_mac(server: dict, endpoints: list[dict] | None = None) -> bool:
    """Return whether a network server is not External and every call base it can use is loopback.

    ``server`` is a ``network_servers`` row (``id``, ``kind``, ``port``, ``metadata_json``).
    A server with no known endpoint, an unreadable base or one non-loopback endpoint is not
    on this Mac: a LAN address can be chosen once the loopback one goes stale.
    """
    if str(server.get("kind") or "") == "external":
        return False
    try:
        from llm import network_endpoints as ne
        from llm.network_discovery import parse_server_metadata

        server_id = str(server.get("id"))
        meta = parse_server_metadata(server_id, server.get("metadata_json"))
        known = ne.get_endpoints(server_id) if endpoints is None else endpoints
        if not known:
            return False
        bases = [
            ne.endpoint_base(meta, host=str(e["host"]), port=server.get("port"))
            for e in known
        ]
        return all(base and endpoint_on_this_mac(base) for base in bases)
    except Exception as error:
        from loguru import logger

        # Fail closed: a server whose endpoints can't be read is not treated as local.
        logger.warning("server locality could not be read for {}: {}", server.get("id"), error)
        return False


@dataclass(frozen=True, slots=True)
class ServerLocality:
    """One network server's locality facts, as every consumer reads them."""

    private: bool
    on_this_mac: bool


def server_locality(server: dict, endpoints: list[dict] | None = None) -> ServerLocality:
    """The one owner of a server's private mark and on-this-Mac fact.

    Catalog rows, send enforcement and the Settings view all read these two facts here,
    so none can drift from another. The mark is private only when it is literally ``true``.
    Pass ``endpoints`` the caller already loaded to avoid a read; ``None`` reads them.
    """
    from llm.network_discovery import parse_server_metadata

    metadata = parse_server_metadata(str(server.get("id")), server.get("metadata_json"))
    return ServerLocality(
        private=bool(metadata and metadata.get("private") is True),
        on_this_mac=server_on_this_mac(server, endpoints),
    )


def row_is_local(
    row: dict, *, private_servers: set[str] | None = None, local_servers: set[str] | None = None,
) -> bool:
    """Return whether a public catalog row is eligible under local-only policy.

    A row naming a server (``omlx@``/``external@``) takes the server's facts: its private mark
    or ``server_on_this_mac`` (passed in as ``local_servers``). Only a network row with no
    server id reads its endpoint from the provider params.
    """
    connection = str(row.get("connection") or row.get("harness") or "")
    backend = str(row.get("backend") or "")
    provider = str(row.get("provider") or "").lower()
    if connection in {"claude-code", "codex"} or backend != "network":
        return False

    server_id = _server_id(row)
    if server_id:
        return server_id in (private_servers or set()) or server_id in (local_servers or set())
    selector = str(row.get("litellm_model") or row.get("selector") or "")
    if "external" in {provider, connection}:
        return False

    try:
        from llm.capabilities import make_provider_params

        api_base = make_provider_params(selector).get("api_base")
        return endpoint_on_this_mac(api_base)
    except Exception as error:
        from loguru import logger

        logger.warning("locality endpoint could not be read: {}", error)
        return False


def project_cloud_models_off(project_id: str | None, *, db) -> bool:
    """Read the project-local cloud switch; an absent row means cloud models are allowed."""
    if not project_id:
        return False
    row = db.execute(
        "SELECT cloud_models_off FROM project_inference_policies WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return bool(row and row["cloud_models_off"])


def cloud_off_project_ids(*, db) -> set[str]:
    """Every project that turned cloud models off, in one read, for views that cover all projects."""
    return {
        str(row["project_id"])
        for row in db.execute(
            "SELECT project_id FROM project_inference_policies WHERE cloud_models_off = 1"
        )
    }


def set_project_cloud_models_off(project_id: str, *, cloud_models_off: bool, db=None) -> dict:
    """Set the sole project cloud-model policy and return its public shape.

    The policy is an idempotent boolean set by a person's own session, so the write takes no
    compare-and-set revision: two writers can only agree on the last value, never lose an update.
    Held inputs are re-offered by the caller's catalog notification, whose claim re-resolves them.
    """
    from connections.vendor_accounts import AccountError
    from infra.database import write_transaction

    with write_transaction(db, scope="project_cloud_models_off") as connection:
        if not connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise AccountError("project does not exist")
        connection.execute(
            """INSERT INTO project_inference_policies(project_id, cloud_models_off)
               VALUES (?, ?)
               ON CONFLICT(project_id) DO UPDATE SET cloud_models_off = excluded.cloud_models_off""",
            (project_id, int(cloud_models_off)),
        )
    return {"project_id": project_id, "cloud_models_off": cloud_models_off}


#: Every refusal ``locality_refusal`` can name; holds, their release and the stalled kind key on it.
LOCALITY_REFUSALS = ("project_cloud_off", "thread_local_only")


def locality_cause(*, thread_local_only: bool, cloud_off: bool) -> str | None:
    """Name the policy that refuses cloud rows, prioritizing a thread's own scope."""
    if thread_local_only:
        return "thread_local_only"
    return "project_cloud_off" if cloud_off else None


def locality_refusal(row: dict, *, thread_local_only: bool, project_id, db) -> str | None:
    """Return the named locality refusal for a catalog row, if policy requires it.

    The project policy is read first: with none in force nothing else is read, so a send in
    an ordinary project adds one primary-key read, never a server lookup.
    """
    cloud_off = project_cloud_models_off(project_id, db=db)
    cause = locality_cause(thread_local_only=thread_local_only, cloud_off=cloud_off)
    if cause is None:
        return None
    private_servers: set[str] = set()
    local_servers: set[str] = set()
    server_id = _server_id(row)
    if server_id:
        server = db.execute("SELECT * FROM network_servers WHERE id = ?", (server_id,)).fetchone()
        if server:
            endpoints = [
                dict(e) for e in db.execute(
                    "SELECT * FROM network_server_endpoints WHERE server_id = ?", (server_id,),
                ).fetchall()
            ]
            facts = server_locality(dict(server), endpoints)
            if facts.private:
                private_servers.add(server_id)
            if facts.on_this_mac:
                local_servers.add(server_id)
    if row_is_local(row, private_servers=private_servers, local_servers=local_servers):
        return None
    return cause
