"""Text for the weekly review of a plan's Later tasks."""

LATER_REVIEW_HEAD = "[CORSIA plan] Weekly review: {count} open {task} in Later on your plan."
LATER_REVIEW_ROW = "- {title} (node {node_id}, revision {revision}){tail}"
LATER_REVIEW_UNTIL = " set aside until {until}"
LATER_REVIEW_REASON = ": {reason}"
LATER_REVIEW_MORE = "+{count} more: plan_read(section='later')"
LATER_REVIEW_MOVES = (
    "Close every one no longer needed in one close_node call with node_ids. "
    "Promote one (plan_update edit later=false) only if it is due now and what "
    "it delivers, not its title, fits the project's priorities and your own, read now; no "
    "priorities written means nothing fits. If one doesn't fit, promoting it is "
    "the person's call, not a detail you settle, whatever its size, cost, "
    "urgency or how easily it is undone: ask them now, put any urgency in your "
    "recommendation, and leave it in Later until they answer. An earlier approval "
    "that doesn't fit them doesn't count. Re-rank with reorder, and leave the rest "
    "in Later."
)
