"""Text an agent reads at the moment a Later task comes back into work.

Words only: nothing is refused and nothing is stored. Each line shows only on
its event (a due date on a Later task, the weekly review of Later, a call that
took a task out of Later), never on every turn.
"""

#: The check itself: the person's rule, made at that moment by reading a file.
FIT = (
    "read the project's priorities and yours now (none written means nothing "
    "fits). If it fits, go ahead. If it doesn't, starting it is the person's "
    "call, not a detail you settle, whatever its size, cost, urgency or how "
    "easily it is undone: ask them now, put any urgency in your recommendation, "
    "and don't work it until they answer. An approval that doesn't fit them "
    "doesn't count."
)

#: What a Later task must deliver, shown where fit is judged, so it is judged
#: on the task and not its title. The same bound as a selected task's criteria
#: in a wake (``close_next_bounds.CRITERIA_MIN_BYTES``).
CRITERIA = "Done when: {criteria}"
CRITERIA_CUT = "… [cut; plan_read(node_id='{node_id}') returns the whole task]"
NO_CRITERIA = "none written; plan_read(node_id='{node_id}') returns the whole task"

#: Replaces the scheduled-task wake's moves when the due task is in Later.
DUE_IN_LATER = (
    "It is in Later. {criteria} Judge fit on that, not the title: before you "
    f"start it, {FIT} Start it, schedule it again with a reason, close it, "
    "leave it in Later, or raise a decision."
)

#: Added to a plan_update reply whose call took a task out of Later.
LEFT_LATER = (
    f'"{{title}}" was in Later: before you work it, {FIT} If you ask, '
    "put it back with edit later=true."
)
