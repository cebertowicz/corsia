"""Retained import path for a helper consumed outside the account suites.

The symbol is defined in vendor_account_fixtures; the tests that pinned it now
live in the behaviour-named account modules."""
from vendor_account_fixtures import isolated_shutdown_tasks  # noqa: F401
