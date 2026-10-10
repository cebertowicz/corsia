"""The explicit failure contract for deliberately refused outcome input."""


class OutcomeInputRefusal(ValueError):
    """An input validator deliberately rejected a value; incidental faults are not refusals."""
