"""Stable post-update retry receipts, including completed replacements."""
import json


def replay_result(db, prior, text, node_id, replace, reason):
    """Return the original receipt, refusing reuse for different normalized arguments."""
    meta = json.loads(prior["metadata"] or "{}")
    if (prior["content"] != text or meta.get("node_id") != node_id
            or set(meta.get("post_update_replace", [])) != set(replace)
            or meta.get("post_update_replace_reason") != reason):
        raise ValueError("idempotency_key already belongs to a different call; use a new key.")
    state = db.execute("SELECT result_text FROM message_update_state WHERE message_id=?", (prior["id"],)).fetchone()
    # Existing successful calls predate E3 receipts; preserve their original bytes.
    return state[0] if state and state[0] else f"Update saved as message {prior['id']}."
