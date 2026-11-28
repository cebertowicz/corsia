"""Activity notification reads are bounded to the queue's list cap."""

from tests.test_activity_notifications import _client


def test_document_limit_accepts_3000_and_rejects_3001(isolated_db, monkeypatch):
    from api import attention_notifications

    calls = []
    monkeypatch.setattr(attention_notifications, '_mark_notifications_read', lambda user, docs, chats: calls.append(len(docs)))
    document = {'artifact_id': 'anything', 'through_reply_id': 'reply'}
    client = _client(monkeypatch)
    assert client.post('/api/attention/notifications/read', json={'documents': [document] * 3000}).json() == {'read': 3000}
    response = client.post('/api/attention/notifications/read', json={'documents': [document] * 3001})
    assert response.status_code == 422
    assert calls == [3000]


def test_document_and_chat_limit_is_combined(isolated_db, monkeypatch):
    from api import attention_notifications

    calls = []
    monkeypatch.setattr(attention_notifications, '_mark_notifications_read',
                        lambda user, docs, chats: calls.append((len(docs), len(chats))))
    document = {'artifact_id': 'anything', 'through_reply_id': 'reply'}
    chat = {'thread_id': 'chat', 'through': '2026'}
    client = _client(monkeypatch)
    assert client.post('/api/attention/notifications/read', json={'documents': [], 'chats': [chat] * 3000}).json() == {'read': 3000}
    assert client.post('/api/attention/notifications/read', json={'documents': [document], 'chats': [chat] * 3000}).status_code == 422
    assert client.post('/api/attention/notifications/read', json={'documents': [], 'chats': [chat] * 3001}).status_code == 422
    assert calls == [(0, 3000)]
