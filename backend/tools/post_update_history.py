"""Update identity recovery without disclosing replaced text in history reads."""
from infra.message_update_state import current


def history_updates(db, rows):
    """Decorate active updates with ids; exact old reads point to their live descendant."""
    result = []
    for raw in rows:
        row = dict(raw)
        state = db.execute("SELECT update_kind,replaced_by FROM message_update_state WHERE message_id=?",
                           (row["id"],)).fetchone()
        if state and state["update_kind"]:
            row["update_id"] = row["id"]
            if state["replaced_by"]:
                live = current(db, row["id"])
                row["content"] = (f"Replaced by {live['id']} (message #{live['seq']}). "
                                  "Read that message to recover the current update.")
                row["metadata"] = None
        result.append(row)
    return result
