"""Shared injection signs used by outside-content screens."""

INJECTION_RULES = (
    ("R1", "text addressed to an AI, assistant, agent or model, or that tries to change its role or rules."),
    ("R2", "a command to the reader itself to send, forward, share, delete or change settings or access, phrased as an order to a system or hidden from the person."),
    ("R3", "hidden or encoded text: instructions in comments, invisible text, or an encoded blob."),
    ("R4", "a claim that the person already approved, authorized or confirmed an action, or that no approval is needed."),
    ("R5", "a link or form that asks for a sign-in, a password, a code or access to an account."),
)


def injection_rule_text() -> str:
    return "\n".join(f"{rule}: {text}" for rule, text in INJECTION_RULES)
