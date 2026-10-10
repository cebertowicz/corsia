"""The one rule for whether a CORSIA tool result reports an error."""

from __future__ import annotations


def is_error_result(result: object) -> bool:
    """A CORSIA tool reports failure by returning text that starts with "Error".

    Settlement, dispatch outcomes, the harness bridges and the tool-row
    display all read a result's error state through this predicate.
    """
    return isinstance(result, str) and result.startswith("Error")
