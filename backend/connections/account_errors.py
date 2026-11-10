"""Fixed, safe-to-show refusals from connected email or calendar accounts."""


class AccountError(Exception):
    """A fixed, safe-to-show refusal from a connected email or calendar account."""


class AccountUnavailable(AccountError):
    """A refusal that clears on its own: trying again later can work, and reconnecting doesn't help."""
