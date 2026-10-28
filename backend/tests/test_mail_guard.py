from dataclasses import replace
from datetime import datetime, timedelta, timezone

from tools.mail_guard import Message, check_reply, evidence

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
TARGET = Message("a", "thread-a", "Taylor <taylor@example.test>", "Budget", NOW - timedelta(days=2), "old")


def check(target=TARGET, thread=None, sender=None, body="hello", to=None):
    return check_reply(target, thread or [], sender or [], "me@example.test", body, to or [], NOW, len(sender or []))


def test_evidence_reads_as_a_sentence():
    message = replace(TARGET, date=datetime(2026, 10, 28, 7, 3, tzinfo=timezone.utc), unread=True)
    assert evidence(message) == (
        "Taylor <taylor@example.test> wrote on Oct 28, 2026, 7:03 AM UTC; this message is unread.\n"
        "The text wasn't screened as safe to show; read it in your mail app."
    )


def test_empty_body_stops():
    result = check(body=" ")
    assert len(result.stops) == 1


def test_reply_to_wins_and_different_requested_address_warns():
    target = replace(TARGET, reply_to="Reply <reply@example.test>")
    result = check(target=target, to=["other@example.test"])
    assert result.recipients == ["reply@example.test"]
    assert result.warnings
    multiple = check(target=replace(TARGET, reply_to="One <one@example.test>, Two <two@example.test>"))
    assert multiple.recipients == ["one@example.test", "two@example.test"]


def test_no_reply_stops_without_reply_to():
    assert check(target=replace(TARGET, sender="No Reply <no-reply@example.test>")).stops
    assert not check(target=replace(TARGET, sender="No Reply <no-reply@example.test>", reply_to="ok@example.test")).stops


def test_sent_after_target_stops_without_stale_override():
    sent = replace(TARGET, message_id="sent", sender="Me <me@example.test>", date=NOW)
    result = check(thread=[sent])
    assert result.stops and not result.stale


def test_same_sender_newer_stops_but_other_sender_warns():
    newer = replace(TARGET, message_id="b", date=NOW, body="secret payload")
    result = check(thread=[newer])
    assert result.stale and "secret payload" not in result.stale[0]
    other = replace(newer, sender="Another <another@example.test>")
    assert check(thread=[other]).warnings


def test_cross_thread_same_conversation_unread_recent_stops_otherwise_warns():
    other = replace(TARGET, message_id="b", thread_id="thread-b", date=NOW,
                    subject="Re: Fwd: Budget", unread=True)
    assert check(sender=[other]).stale
    assert check(sender=[replace(other, unread=False)]).warnings
    old = replace(other, date=NOW - timedelta(days=8))
    assert check(target=replace(TARGET, date=NOW - timedelta(days=9)), sender=[old]).warnings
    assert not check(sender=[replace(other, subject="Other")]).stale


def test_same_display_name_at_different_address_is_not_same_sender():
    impostor = replace(TARGET, message_id='other', sender='Taylor <other@example.test>', date=NOW)
    result = check(thread=[impostor])
    assert not result.stale
    assert result.warnings


def test_relay_address_matching_and_email_count():
    relay = replace(TARGET, sender="Taylor <relay@example.test>")
    newer = replace(relay, message_id="b", thread_id="thread-b", date=NOW, unread=True)
    other = replace(newer, message_id="c", thread_id="thread-c", sender="Jordan <relay@example.test>")
    result = check(target=relay, sender=[relay, newer, other])
    assert len(result.stale) == 2
    assert result.info[0].startswith("3 emails")
