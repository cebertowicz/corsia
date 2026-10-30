"""The live state of every connector poller, as ``GET /api/system`` reports it."""

from __future__ import annotations


def live_poller_statuses() -> dict:
    """The non-cached poller state included in each system status response."""
    from events.connectors.github_notifications import poller_status
    from events.connectors.github_pr_quiet_watch import quiet_watch_status
    from events.connectors.github_repo_activity import poller_status as repo_activity_poller_status
    from events.connectors.mail_poller import poller_status as mail_poller_status

    return {
        "github_notifications": poller_status(),
        "github_repo_activity": repo_activity_poller_status(),
        "mail_poller": mail_poller_status(),
        "pr_quiet_watch": quiet_watch_status(),
    }
