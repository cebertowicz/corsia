"""Rows for the Later section of a plan's status."""

from plans import later_review
from plans.close_next_bounds import bounded, title as _title


def rows(db, nodes: list[dict], *, reason_bytes: int) -> list[dict]:
    """Build the page's rows using the same facts as the review wake."""
    facts = later_review.row_facts(db, [node["id"] for node in nodes])
    return [_row(node, facts.get(node["id"], {}), reason_bytes) for node in nodes]


def _row(node: dict, extra: dict, reason_bytes: int) -> dict:
    title = _title(str(node["title"]))
    reason = extra.get("reason")
    reason_text = bounded(str(reason), reason_bytes) if reason else None
    result = {
        "id": node["id"], "title": title, "revision": node["revision"],
        "reason": reason_text, "detail_read": f"plan_read(node_id='{node['id']}')",
    }
    if extra.get("date"):
        result["date"] = extra["date"]
    if title != node["title"] or reason_text != reason:
        result["truncated"] = True
    return result
