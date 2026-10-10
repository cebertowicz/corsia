"""Rule registry for the anti-pattern detector.

Each rule is a single module exposing one `RULE: Rule` instance. Adding a
seventh rule means dropping a new file here and adding it to `ALL_RULES` (or
relying on the auto-discovery — see `discover_rules()`).

Public:
    ALL_RULES — the canonical ordered list of shipping rules.
    discover_rules() — used by the engine to enumerate enabled rules.
"""

from __future__ import annotations

from ._base import Rule
from . import (
    direct_commit_outside_write_transaction,
    fallback_chain_masks_error,
    hardcoded_model_name,
    hardcoded_path_or_id,
    missing_public_docstring,
    mock_where_real_needed,
    periodic_server_reread,
    silent_error_swallow,
    untracked_todo,
)


ALL_RULES: list[Rule] = [
    direct_commit_outside_write_transaction.RULE,
    silent_error_swallow.RULE,
    hardcoded_path_or_id.RULE,
    hardcoded_model_name.RULE,
    fallback_chain_masks_error.RULE,
    missing_public_docstring.RULE,
    mock_where_real_needed.RULE,
    untracked_todo.RULE,
    periodic_server_reread.RULE,
]


def discover_rules() -> list[Rule]:
    """Return all built-in rules in canonical order."""
    return list(ALL_RULES)
