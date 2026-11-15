"""Text for the weekly review of a plan's Later tasks."""

LATER_REVIEW_HEAD = "[CORSIA plan] Weekly review: {count} open {task} in Later on your plan."
LATER_REVIEW_ROW = "- {title} (node {node_id}, revision {revision}){tail}"
LATER_REVIEW_UNTIL = " set aside until {until}"
LATER_REVIEW_REASON = ": {reason}"
LATER_REVIEW_MORE = "+{count} more: plan_read(section='later')"
LATER_REVIEW_MOVES = (
    "Close every one no longer needed in one close_node call with node_ids. "
    "Promote one only if it is due now (plan_update edit later=false), "
    "re-rank with reorder, and leave the rest in Later."
)
