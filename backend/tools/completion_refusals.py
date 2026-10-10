"""What a structured child hears when it sends its own completion (#282).

Its plan's return already carries the result, so the send is refused. What
"the return" is depends on whether the child has a step to put evidence on.
"""

STEPS = (
    "Error: your plan's return already delivers your result to the "
    "parent; nothing was sent. Put the result in your step's review "
    "evidence (register an artifact and name it there if it is long), "
    "then stop"
)

PLANLESS = (
    "Error: your plan's return already delivers your result to the "
    "parent; nothing was sent. You have no step to put review "
    "evidence on: end the turn with the result as your final reply, "
    "which returns to the parent for review (register an artifact "
    "and name it there if it is long)"
)
