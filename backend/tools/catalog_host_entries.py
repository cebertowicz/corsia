"""Host-access metadata for the agent-facing tool catalog."""

from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

_Entry = ToolCatalogEntry


HOST_ACCESS_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "audit_read": _Entry(
        "host_access", authority="scoped-audit-read",
        summary="Read bounded caller-owned audit metrics without a host lease",
        activity="Reading audit log",
    ),
    "log_read": _Entry(
        "host_access", authority="conductor-log-read",
        summary=(
            "Conductors: read CORSIA's backend logs without a lease; other "
            "threads receive a final not_conductor refusal"
        ),
        activity="Reading CORSIA logs",
    ),
    "request_host_access": _Entry(
        "host_access", status="substrate", authority="host-access-request",
        summary="Request a reviewed, temporary host-access lease",
        activity="Requesting host access",
    ),
    "release_host_access": _Entry(
        "host_access", status="substrate", authority="host-access-release",
        summary="End this thread's host-access lease",
        activity="Releasing host access",
    ),
    "managed_process_status": _Entry(
        "host_access", authority="managed-process-read",
        summary="Check a background process started by bash",
        activity="Checking process",
    ),
    "managed_process_logs": _Entry(
        "host_access", authority="managed-process-read",
        summary="Read a background process's logs",
        activity="Reading process logs",
    ),
    "managed_process_list": _Entry(
        "host_access", authority="managed-process-read",
        summary="List this thread's background processes",
        activity="Listing processes",
    ),
    "managed_process_stop": _Entry(
        "host_access", authority="managed-process-stop",
        summary="Stop a background process",
        activity="Stopping process",
    ),
    "process_discover": _Entry(
        "host_access", authority="host-process-read",
        summary="Find host processes by name or PID",
        activity="Finding processes",
    ),
    "process_signal": _Entry(
        "host_access", authority="host-process-signal",
        summary="Signal a host process found by process_discover",
        activity="Signaling process",
    ),
}
