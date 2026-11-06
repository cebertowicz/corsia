"""Select comment threads for the agent-facing document listing.

Open means what the person sees as open: not resolved. A decided suggestion that already had
replies stays unresolved so its conversation can go on (``comment_actions._record_decision_row``),
and the agent answering the person must see it; its ``decision`` field says it is decided.
"""


def select_comment_threads(comments: list[dict], status: str, comment_id: str | None) -> dict:
    if comment_id is not None:
        selected = [thread for thread in comments if thread.get("id") == comment_id
                    or any(reply.get("id") == comment_id for reply in thread.get("replies", []))]
        result = {"comments": selected, "closed_count": 0}
        if not selected:
            result["note"] = "No comment with that id on this document."
        return result
    if status == "all":
        return {"comments": comments, "closed_count": 0}
    selected = [thread for thread in comments if not thread.get("resolved")]
    closed_count = len(comments) - len(selected)
    result = {"comments": selected, "closed_count": closed_count}
    if closed_count:
        result["note"] = (
            f"{closed_count} resolved comment threads not shown; "
            "pass status='all' to include them."
        )
    return result
