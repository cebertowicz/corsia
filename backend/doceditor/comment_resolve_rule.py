"""Who resolves a person's comment: one rule, told to agents by the feedback-node framing and
doc_comment_update. A leaf module, so plans can import it without a cycle through doceditor."""

PERSON_COMMENT_RESOLVE_RULE = (
    "Leave the user's comment open for them to resolve, above all one you replied to; resolve it "
    "yourself only when they asked you to, or when you made exactly the change it asked for "
    "and had nothing to say."
)
