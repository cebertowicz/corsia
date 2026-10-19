"""Platform event delivery: wake vs digest by declared fact rule, idempotency,
dead-letter, and designation-following binder rules."""

from __future__ import annotations

import pytest

from connections import binder
from events.envelope import normalize
from events.platform import (
    DEAD_LETTERED, DIGESTED, DUPLICATE, WOKE, deliver_platform_event_sync,
)
from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
from infra.database import get_db
from tests.event_routing_helpers import mk_session

SCREENED_NOW = {"screen_status": "screened", "injection": False, "needs_person": "now"}


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _mail(message_id="m1", thread_id="t1", **screen):
    return {"account_id": "acct-1", "from": "a@example.com", "to": "me@example.com", "label": "INBOX",
            "thread_id": thread_id, "message_id": message_id,
            "subject": "Hello", **screen}


@pytest.fixture
def workspace(isolated_db):
    thread = mk_session("ws")
    assert set_scope_coordinator(WORKSPACE_SCOPE, thread)
    assert binder.ensure_designation_rule(
        connector="gmail", fact="label", value="INBOX") is not None
    return thread


def _count(sql, *args):
    db = get_db()
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


def _wakes(thread):
    return _count(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'",
        thread)


def _notes(thread):
    return _count(
        "SELECT COUNT(*) FROM context_change_journal "
        "WHERE object_kind='thread_context' AND object_id=?", thread)


class TestDeclaredRule:
    def test_default_informational_and_rule_promotes(self):
        assert normalize("gmail", _mail()).event_class == "informational"
        assert normalize("gmail", _mail(**SCREENED_NOW)).event_class == "actionable"

    @pytest.mark.parametrize("override", [
        {"screen_status": "unavailable"}, {"screen_status": "pending"},
        {"injection": True}, {"needs_person": "today"}, {"needs_person": "no"},
    ])
    def test_flagged_or_unscreened_is_never_actionable(self, override):
        raw = _mail(**{**SCREENED_NOW, **override})
        assert normalize("gmail", raw).event_class == "informational"

    def test_missing_screen_facts_fail_closed(self):
        raw = _mail(screen_status="screened", needs_person="now")  # no injection fact
        assert normalize("gmail", raw).event_class == "informational"


class TestDelivery:
    def test_flagged_and_unscreened_mail_never_wakes(self, workspace):
        for i, screen in enumerate((
            {**SCREENED_NOW, "injection": True},
            {**SCREENED_NOW, "screen_status": "unavailable"},
        )):
            out = deliver_platform_event_sync("gmail", _mail(f"m{i}", f"t{i}", **screen))
            assert [d.outcome for d in out] == [DIGESTED]
        assert _wakes(workspace) == 0
        assert _notes(workspace) == 2

    def test_now_message_wakes_exactly_once(self, workspace):
        first = deliver_platform_event_sync("gmail", _mail(**SCREENED_NOW))
        again = deliver_platform_event_sync("gmail", _mail(**SCREENED_NOW))
        assert [d.outcome for d in first] == [WOKE]
        assert [d.outcome for d in again] == [DUPLICATE]
        assert _wakes(workspace) == 1

    def test_repeat_digest_is_a_noop(self, workspace):
        raw = _mail(screen_status="screened", injection=False, needs_person="today")
        assert [d.outcome for d in deliver_platform_event_sync("gmail", raw)] == [DIGESTED]
        assert [d.outcome for d in deliver_platform_event_sync("gmail", raw)] == [DUPLICATE]
        assert _notes(workspace) == 1

    def test_digest_never_wakes(self, workspace):
        raw = _mail(screen_status="screened", injection=False, needs_person="no")
        deliver_platform_event_sync("gmail", raw)
        assert _wakes(workspace) == 0
        assert _count(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?", workspace) == 0

    def test_digest_notes_of_one_email_thread_collapse(self, workspace):
        from infra.passive_digest import _collapse_key

        for mid in ("m1", "m2"):
            deliver_platform_event_sync(
                "gmail", _mail(mid, "t1", screen_status="screened",
                               injection=False, needs_person="no"))
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM context_change_journal WHERE object_kind='thread_context'"
            ).fetchall()
            assert len(rows) == 2
            assert len({_collapse_key(r) for r in rows}) == 1
        finally:
            db.close()

    def test_unresolvable_target_dead_letters_once(self, isolated_db):
        raw = _mail(**SCREENED_NOW)  # no rule, no owner
        first = deliver_platform_event_sync("gmail", raw)
        again = deliver_platform_event_sync("gmail", raw)
        assert [d.outcome for d in first] == [DEAD_LETTERED]
        assert [d.outcome for d in again] == [DUPLICATE]
        assert _count("SELECT COUNT(*) FROM dead_letter_events") == 1


class TestDesignationRule:
    def test_designation_move_repoints_rule(self, workspace):
        other = mk_session("ws2")
        assert set_scope_coordinator(WORKSPACE_SCOPE, other)
        [rule] = [r for r in binder.list_rules("gmail") if r.fact == "label"]
        assert rule.destination_thread_id == other
        out = deliver_platform_event_sync("gmail", _mail("m9", "t9", **SCREENED_NOW))
        assert [(d.outcome, d.thread_id) for d in out] == [(WOKE, other)]

    def test_ensure_is_idempotent_and_ignores_plain_rules(self, workspace):
        a = binder.ensure_designation_rule(connector="gmail", fact="label", value="INBOX")
        assert a is not None
        assert len(binder.list_rules("gmail")) == 1
        plain = binder.create_rule(
            connector="gmail", fact="from", value="x@example.com",
            destination_thread_id=workspace)
        other = mk_session("ws3")
        set_scope_coordinator(WORKSPACE_SCOPE, other)
        rules = {r.id: r for r in binder.list_rules("gmail")}
        assert rules[plain.id].destination_thread_id == workspace

    def test_cleared_designation_makes_rule_unroutable(self, workspace):
        set_scope_coordinator(WORKSPACE_SCOPE, None)
        assert binder.list_rules("gmail") == []
        out = deliver_platform_event_sync("gmail", _mail("m8", "t8", **SCREENED_NOW))
        assert [d.outcome for d in out] == [DEAD_LETTERED]
