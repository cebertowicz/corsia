"""The built-in account's seeded name is one constant that its label rule reads back."""
from __future__ import annotations


def test_seeded_builtin_name_is_the_constant_the_label_rule_compares(isolated_db):
    from connections.account_profiles import account_label
    from connections.vendor_accounts import DEFAULT_ACCOUNT_NAME
    from vendor_account_fixtures import seed_default_account

    builtin = seed_default_account("claude-code")
    assert builtin["name"] == DEFAULT_ACCOUNT_NAME
    assert account_label(builtin)["custom"] is None
    assert account_label({**builtin, "name": DEFAULT_ACCOUNT_NAME + " renamed"})["custom"] == DEFAULT_ACCOUNT_NAME + " renamed"
