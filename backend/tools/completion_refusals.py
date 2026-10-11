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

#: The harvest takes the Turn's last message as the return, so a sign-off
#: written after the result would replace it.
PLANLESS = (
    "Error: your plan's return already delivers your result to the "
    "parent; nothing was sent. You have no step, so your last message "
    "this turn is the return: end the turn with the result as that "
    "message and write nothing after it. If the result is long, "
    "register an artifact and name it in that message"
)
