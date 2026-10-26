"""One-time agent guidance when a thread comes to own a GitHub pull request.

Three triggers (design §3.1): the first tool result after ownership begins, a
literal polling command while the thread owns an open PR (at most once per
30 min), and the first routed event for a PR bound without a tool call. A
``pr_notice:<thread>:<external_id>`` settings marker makes the ownership
notice once per owner; a rehomed PR gets its own notice for the new owner.

The owned-PR read is one query, repeated at most every 60 s per thread, except
right after a call that can create or register a PR.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.retirement_predicate import ENDED_PULL_REQUEST

NOTICE_PREFIX = "pr_notice:"
POLL_NOTICE_INTERVAL_S = 30 * 60
QUERY_GUARD_S = 60
_CACHE_MAX = 512
_POLL_COMMAND = re.compile(r"^gh\s+(?:pr\s+(?:checks|view)|run\s+(?:watch|view))\b")
_CREATES_PR = re.compile(r"\bgh\s+pr\s+create\b")
_REGISTERING_TOOLS = frozenset({"artifact_register"})
_OWNER_STATES = ", ".join(f"'{state}'" for state in OWNER_REGISTRATION_STATES)

#: Owner registrations of active, not-ended GitHub PRs, pinned owners first.
#: ``thread_filter`` is ``""`` or ``"AND r.parent_thread_id=?"``.
OWNED_PR_SQL = f"""SELECT r.parent_thread_id, i.external_id,
          EXISTS(SELECT 1 FROM settings AS s
                  WHERE s.key='{NOTICE_PREFIX}' || r.parent_thread_id || ':' || i.external_id)
          AS noticed
     FROM artifact_registrations AS r
     JOIN artifact_identities AS i ON i.id=r.artifact_id
    WHERE r.relationship_state IN ({_OWNER_STATES})
      AND i.state='active'
      AND i.integration='{GITHUB_INTEGRATION}' AND i.type='{PR_TYPE}'
      AND NOT {ENDED_PULL_REQUEST}
      {{thread_filter}}
    ORDER BY CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
             r.updated_at DESC"""


@dataclass(frozen=True, slots=True)
class OwnedPr:
    external_id: str
    repo: str
    number: int
    noticed: bool


@dataclass(slots=True)
class _OwnedRead:
    timestamp: float
    rows: list[OwnedPr] | None = None


_owned_cache: dict[str, _OwnedRead] = {}
_last_poll_notice: dict[str, float] = {}


def pr_event_notice(
    *, thread_id: str | None, tool_name: str, arguments: Any,
) -> str | None:
    """Return the ownership notice once per owned PR, or the poll reminder, else ``None``."""
    if not thread_id:
        return None
    command = _bash_command(tool_name, arguments)
    fresh = tool_name in _REGISTERING_TOOLS or bool(command and _CREATES_PR.search(command))
    owned = _owned_prs(thread_id, fresh=fresh)
    pending = next((pr for pr in owned if not pr.noticed), None)
    if pending is not None:
        return _claim(thread_id, pending)
    if not owned or command is None or _POLL_COMMAND.match(command) is None:
        return None
    now = time.monotonic()
    previous = _last_poll_notice.get(thread_id)
    if previous is not None and now - previous < POLL_NOTICE_INTERVAL_S:
        return None
    if len(_last_poll_notice) >= _CACHE_MAX:
        _last_poll_notice.clear()
    _last_poll_notice[thread_id] = now
    return _notice(_polled_pr(owned, command))


def notice_for_routed_event(thread_id: str, identity: Any) -> str | None:
    """Claim the ownership notice for a routed event on a PR this thread owns."""
    if (
        identity is None
        or getattr(identity, "integration", None) != GITHUB_INTEGRATION
        or getattr(identity, "type", None) != PR_TYPE
    ):
        return None
    pr = next(
        (pr for pr in _owned_prs(thread_id, fresh=True)
         if pr.external_id == identity.external_id),
        None,
    )
    return None if pr is None or pr.noticed else _claim(thread_id, pr)


def release_notice(thread_id: str, identity: Any) -> None:
    """Undo a routed event's claim when its delivery failed, so the next one carries it."""
    from infra.database import write_transaction

    with write_transaction(None, scope="pr_notice_release") as connection:
        connection.execute(
            "DELETE FROM settings WHERE key=?",
            (f"{NOTICE_PREFIX}{thread_id}:{identity.external_id}",),
        )
    _owned_cache.pop(thread_id, None)


def webhook_configured() -> bool:
    """Whether GitHub webhooks can reach this instance: the endpoint's own secret read."""
    from api.webhook_receivers import receiver_secret
    from events.connectors.github_receiver import GITHUB_RECEIVER

    return bool(receiver_secret(GITHUB_RECEIVER))


def parse_pr_external_id(external_id: str) -> tuple[str, int] | None:
    """Split the canonical ``owner/repo#number`` pull-request identity."""
    repo, separator, raw_number = external_id.rpartition("#")
    if not separator or not repo or not raw_number.isdigit():
        return None
    return repo, int(raw_number)


def _owned_prs(thread_id: str, *, fresh: bool) -> list[OwnedPr]:
    now = time.monotonic()
    cached = _owned_cache.get(thread_id)
    if (not fresh and cached is not None and cached.rows is not None
            and now - cached.timestamp < QUERY_GUARD_S):
        return cached.rows
    from infra.database import get_db

    entry = _OwnedRead(now)
    if len(_owned_cache) >= _CACHE_MAX:
        _owned_cache.clear()
    _owned_cache[thread_id] = entry
    try:
        rows = get_db().execute(
            OWNED_PR_SQL.format(thread_filter="AND r.parent_thread_id=?"), (thread_id,),
        ).fetchall()
        owned = []
        for row in rows:
            parsed = parse_pr_external_id(row["external_id"])
            if parsed is not None:
                owned.append(OwnedPr(row["external_id"], *parsed, bool(row["noticed"])))
        # Invalidation detaches this entry; an older query never reinstalls it.
        entry.rows = owned
        return owned
    finally:
        if entry.rows is None and _owned_cache.get(thread_id) is entry:
            _owned_cache.pop(thread_id, None)


def _claim(thread_id: str, pr: OwnedPr) -> str | None:
    """Render first, then let the canonical marker writer select one claimant."""
    from infra.database import write_transaction
    from infra.settings import set_setting

    notice = _notice(pr)
    key = f"{NOTICE_PREFIX}{thread_id}:{pr.external_id}"
    with write_transaction(None, scope="pr_notice_claim") as connection:
        claimed = connection.execute(
            "SELECT 1 FROM settings WHERE key=?", (key,),
        ).fetchone() is None
        if claimed:
            set_setting(key, {"noticed_at": time.time()}, db=connection)
    _owned_cache.pop(thread_id, None)
    return notice if claimed else None


def _bash_command(tool_name: str, arguments: Any) -> str | None:
    if tool_name != "bash" or not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    return command.lstrip() if isinstance(command, str) else None


def _polled_pr(owned: list[OwnedPr], command: str) -> OwnedPr:
    """The owned PR the command names by number, else the most recent one."""
    numbers = {int(token) for token in re.findall(r"(?<![\w/])(\d+)\b", command)}
    return next((pr for pr in owned if pr.number in numbers), owned[0])


def _watched(pr: OwnedPr) -> bool:
    """Whether CORSIA watches this PR's repository (the broker's cached tracked set).

    An unreadable set counts as watched: the notice never claims a repository
    is untracked on a guess.
    """
    from connections.github_broker import known_tracked_repositories

    known = known_tracked_repositories()
    return known is None or pr.repo.lower() in {name.lower() for name in known}


def _notice(pr: OwnedPr) -> str:
    """The design §4 wording for this instance's delivery (webhook or poller only)."""
    if not _watched(pr):
        return (
            f"[CORSIA PR notice] You own PR {pr.repo}#{pr.number}. CORSIA does not watch "
            f"{pr.repo}, so it will not wake you when this PR's CI or review is overdue. "
            f"Check its state yourself when you need it, for example with `gh pr checks {pr.number}`."
        )
    if webhook_configured():
        return (
            f"[CORSIA PR notice] You own PR {pr.repo}#{pr.number}. CORSIA wakes you when "
            "its CI finishes, when the reviewer gives a verdict, and when someone comments. "
            "Do not poll `gh pr checks` or `gh pr view`. End your turn or do other work. "
            "If an expected result is late, CORSIA wakes you once and says what it found."
        )
    return (
        f"[CORSIA PR notice] You own PR {pr.repo}#{pr.number}. CORSIA wakes you when "
        "someone comments. This instance does not receive CI or reviewer results. When you "
        f"need them, run `gh pr checks {pr.number}` once. Do not poll in a loop."
    )
