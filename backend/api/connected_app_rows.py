"""Safe Connected Apps list-row projections for people and agents."""
from __future__ import annotations

#: The one notice for an app that runs on this Mac: shown when it is connected, approved or brought over (B-R22).
LOCAL_APP_NOTICE = (
    "It can do anything you can do on this Mac, except open CORSIA's own files and saved keys. "
    "Connect only apps you trust."
)


def local_app_notice(config: object) -> str | None:
    return LOCAL_APP_NOTICE if isinstance(config, dict) and isinstance(config.get("command"), str) else None


def _transport(config: object) -> str | None:
    if not isinstance(config, dict):
        return None
    if isinstance(config.get("command"), str):
        return "stdio"
    if isinstance(config.get("url"), str):
        return "http"
    return None


def _agent_next_step(
    name: object,
    state: object,
    failure_class: object,
    key_refs: list[dict[str, object]],
    *,
    on_here: bool,
    place: str = "Settings › Connected apps",
) -> str | None:
    """Return the fixed person-facing recovery step without exposing a key value."""
    label = name if isinstance(name, str) else "This app"
    if not on_here:
        return f"{label} is turned off for this project; ask the person to turn it on in Connected apps."
    if state == "awaiting_approval":
        return f"Ask the person to approve {label} in {place}."
    if state == "expired":
        return (
            f"My request to connect {label} expired after seven days without an answer. "
            "Do you still want it? If so, I'll send it again."
        )
    if state == "connecting":
        return "Wait for the connection attempt to finish."
    if state == "working":
        return f'Load its tools with tools_load(server="{label}").'
    if state == "needs_token":
        return (
            f"{label} needs a key. Ask the person to open Settings › Keys, find the {label} key card, and press Set. "
            "Please don't paste it here."
        )
    if state == "token_rejected":
        return (
            f"{label} didn't accept its key. Ask the person to open Settings › Keys, find the {label} key card, "
            "and press Replace."
        )
    if state == "needs_sign_in":
        return f"Ask the person to press Sign in next to {label} in {place}. Please don't try to sign in yourself."
    if state == "disconnected":
        return f"{label} lost its connection. Open {label} in {place} and press Reconnect."
    if state == "failed":
        detail = {
            "cannot_start": "app didn't start",
            "timed_out": "no response",
            "protocol_error": "not compatible",
            "name_collision": "clashes with another app",
        }.get(failure_class, "an unknown reason")
        return (
            f"Reconnect {label} once. If it fails again, tell the person: {label} couldn't connect: {detail}. "
            f"I tried once more and it still didn't connect. Open {label} in {place} to see why."
        )
    return None


def server_row(app: dict[str, object], *, on_here: bool, full: bool, db, person: bool = False) -> dict[str, object]:
    """Return a value-free app projection, limiting unreachable agent rows."""
    if app["state"] in {"awaiting_approval", "expired"}:
        live = {"state": app["state"], "state_since": app["state_since"], "tool_count": 0, "failure_class": None}
    else:
        from tools.mcp_runtime import connected_app_state

        live = connected_app_state(app["id"])
    if not full:
        return {
            "name": app["name"], "state": live["state"], "on_here": False,
            "next_step": _agent_next_step(app["name"], live["state"], None, [], on_here=False),
        }
    config = app["config"]
    assert isinstance(config, dict)
    from connections.app_reach import key_ref_descriptors

    key_refs = key_ref_descriptors(app["id"], config, db=db)
    from api.connected_app_signin_rows import sign_in_view

    sign_in = sign_in_view(config, app["id"], person=person, db=db)
    return {
        "id": app["id"],
        "name": app["name"],
        "transport": _transport(config),
        "state": live["state"],
        "state_since": live["state_since"] or app["state_since"],
        "tool_count": live["tool_count"],
        "failure_class": live["failure_class"],
        "next_step": _agent_next_step(
            app["name"], live["state"], live["failure_class"], key_refs, on_here=on_here,
        ),
        "key_refs": key_refs,
        "config": {key: value for key, value in config.items() if key != "key_refs"},
        "pending_config": app["pending_config"],
        "pending_actor_thread_id": app["pending_actor_thread_id"],
        "pending_actor_name": app["pending_actor_name"],
        "created_by_thread_id": app["created_by_thread_id"],
        "created_at": app["created_at"],
        "revision": app["revision"],
        "reach": app["reach"],
        "on_here": on_here,
        **({"notice": notice} if app["state"] == "awaiting_approval" and (
            notice := local_app_notice(app["pending_config"] or config)
        ) else {}),
        **({"folder": app["source_folder"]} if app.get("source_folder") else {}),
        **({"sign_in": sign_in} if sign_in is not None else {}),
    }


def app_reaches_project(app: dict[str, object], project_id: str | None) -> bool:
    """Whether a persisted reach policy includes a project or the workspace."""
    reach = app["reach"]
    assert isinstance(reach, dict)
    if reach["mode"] == "all":
        return True
    return bool(reach["outside_projects"]) if project_id is None else project_id in reach["project_ids"]
