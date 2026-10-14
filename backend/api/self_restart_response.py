"""Response and audit presentation for the self-restart route."""

from __future__ import annotations


def emit_self_restart_audit(
    auth,
    user: dict,
    requester_thread_id: str | None,
    reason: str | None,
    redeploy: bool,
) -> None:
    """Emit the analytics twin of the durable self_restart_requests audit row."""
    from infra.product_events import emit_product_event

    emit_product_event(
        "self_restart",
        "requested",
        thread_id=requester_thread_id,
        user_id=user.get("id"),
        properties={
            "serving_sha": auth.head_sha,
            "serving_short_sha": auth.short_sha,
            "branch": auth.branch,
            "serving_root": auth.serving_root,
            "requester_thread_id": requester_thread_id,
            "reason": reason,
            "redeploy": redeploy,
        },
    )


def self_restart_response(auth, redeploy: bool) -> dict:
    """Return the acknowledged self-restart response."""
    action = (
        "pull origin (--ff-only, current branch), rebuild, then drain and restart"
        if redeploy
        else "drain and restart the current on-disk build"
    )
    return {
        "ok": True,
        "redeploy": redeploy,
        "serving_sha": auth.short_sha,
        "serving_root": auth.serving_root,
        "message": (
            f"Restart acknowledged. This instance will {action}; this connection "
            f"drops during the bounce. A dirty, detached, or non-fast-forward "
            f"serving checkout fails closed before any bounce. Your Turn will "
            f"receive a restart-pending notice shortly; call restart_resume at "
            f"your next safe boundary if unfinished work must continue after "
            f"startup."
        ),
    }
