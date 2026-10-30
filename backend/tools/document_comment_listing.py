"""Select comment threads for the agent-facing document listing."""

from doceditor.suggestion_overlap import is_open_thread


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
    selected = [thread for thread in comments if is_open_thread(thread)]
    closed_count = len(comments) - len(selected)
    result = {"comments": selected, "closed_count": closed_count}
    if closed_count:
        result["note"] = (
            f"{closed_count} resolved or decided comment threads not shown; "
            "pass status='all' to include them."
        )
    return result
