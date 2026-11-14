"""Shared replacement guidance and exact tool result/refusal text."""
FOLLOW_UP = "post a new update with only what changed, without repeating the rest"
REPLACE_RULE = (
    "Replace your own unseen updates when their subject changes; once one is seen, "
    "send a new update with only what changed; a new subject needs no replace."
)
GUIDANCE = (
    "Replace an unseen update when its subject changes; carry forward everything still true "
    "and everything the person must know, including what was done, decided, or at risk. "
    f"A new subject needs no replace. For a seen update, {FOLLOW_UP}."
)
FULL_TEXT_BUDGET = 2000
FIRST_LINE_CHARS = 120
REASONS = {"presence_at_post": "presence at post", "visit": "chat opened",
           "mark_read": "Mark read", "comment": "person commented", "presence_now": "presence live now"}


def refuse(message):
    """One rollback prefix shared by every validation/refusal branch."""
    return f"Error: Nothing was saved. {message}"


def local_time(stamp):
    """Render a stored timestamp in the install's local timezone."""
    from infra.presence_time import parse
    from datetime import UTC

    return parse(stamp).replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def seen_refusal(message_id, verdict):
    """Name known viewing evidence or explicitly say unknown counts as seen."""
    if verdict.reason == "unknown":
        return (f"Whether {message_id} was seen is unknown ({verdict.unknown_reason}); "
                f"it counts as seen. {FOLLOW_UP.capitalize()}. A new subject needs no replace.")
    return (f"{message_id} was seen at {local_time(verdict.at)} ({REASONS[verdict.reason]}). "
            f"{FOLLOW_UP.capitalize()}. A new subject needs no replace.")


def first_line(text):
    """An older row's first line, cut so a one-paragraph update stays one short row."""
    line = text.split("\n", 1)[0].removesuffix("\r")
    return line if len(line) <= FIRST_LINE_CHARS else line[:FIRST_LINE_CHARS - 1] + "…"


def result_text(message_id, replaced, rows, thread_id):
    """Render newest full texts within the cap, then the older first lines."""
    texts = [row["content"] for row in rows]
    full = [True] * len(rows)
    total = sum(map(len, texts))
    for index in range(len(rows) - 1, -1, -1):
        if total <= FULL_TEXT_BUDGET:
            break
        full[index] = False
        total -= len(texts[index])
    lines = [f"Update saved as message {message_id}.",
             f"Replaced: {', '.join(replaced) if replaced else 'none'}.",
             f"Other unseen updates: {len(rows)}."]
    for row, text, complete in zip(rows, texts, full):
        body = text if complete else first_line(text)
        lines.append(f"{row['message_id']} | #{row['seq']} | {local_time(row['posted_at'])} | {body}")
    if not all(full):
        lines.append(f"Oldest updates show their first line, up to {FIRST_LINE_CHARS} characters. Read one in full with "
                     f'get_thread_history(thread_id="{thread_id}", from_seq=N, to_seq=N, tool_output="full").')
    lines.append(GUIDANCE)
    return "\n".join(lines)


def replacement_args(kwargs):
    """Validate the all-or-nothing replacement input without inventing a default reason."""
    ids = kwargs.get("replace", [])
    if not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() for item in ids):
        raise ValueError("replace must be a list of distinct, non-empty message ids; omit it for a new subject.")
    if len(ids) != len(set(ids)):
        raise ValueError("replace must be a list of distinct, non-empty message ids; omit it for a new subject.")
    reason = kwargs.get("replace_reason")
    if (ids and reason not in ("corrects", "updates")) or (not ids and reason is not None):
        raise ValueError('Pass replace_reason="corrects" or "updates" with replace; omit replace_reason when nothing is replaced.')
    return ids, reason
