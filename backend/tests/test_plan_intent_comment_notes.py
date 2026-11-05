"""Chat comment notes remain sources for correction notifications."""
from types import SimpleNamespace

from jobs import corrections_realtime, corrections_recording


def test_a_comment_only_reply_notifies_the_corrections_worker(monkeypatch):
    notified = []
    monkeypatch.setattr(corrections_realtime, "notify", lambda kind, source_id: notified.append((kind, source_id)))
    comments = {"reply_context": {"comments": [{"quote": "agent wrote", "note": "Fix the reply."}]}}

    def send(message_id: str, content: str, metadata: object) -> None:
        corrections_recording.notify_if_owner(
            SimpleNamespace(message_id=message_id, duplicate=False, input_order=1),
            role="user", content=content, metadata=metadata,
        )

    send("comments-only", "", comments)
    send("typed", "Typed words.", comments)
    send("relayed", "", {**comments, "sender_kind": "agent"})

    assert notified == [
        ("chat_comment", "c1.1:comments-only"),
        ("message", "typed"),
        ("chat_comment", "c1.1:typed"),
    ]
