"""Whether an event's facts pass a rule's tests.

A test on the connector's repository fact compares without case, the same as
``create`` validates it against the project's repositories: a webhook keeps
the owner's and repository's casing, and the project's remotes are lowercase.
A connector may declare other facts' comparison (``fact_comparison``): without
case, or as a domain that also matches its subdomains. Names no connector.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from events.connectors import ConnectorManifest
    from events.rules import Rule

EXACT = "exact"
CASELESS = "caseless"
DOMAIN = "domain"


def _manifest(connector: str) -> ConnectorManifest | None:
    from events.connectors import get_manifest

    try:
        return get_manifest(connector)
    except KeyError:
        return None


def repository_fact(connector: str) -> str | None:
    """The fact naming an event's repository, if the connector declares one."""
    manifest = _manifest(connector)
    return manifest.repository_fact if manifest is not None else None


def _same(actual: Any, expected: Any, comparison: str) -> bool:
    if isinstance(expected, str) and not isinstance(actual, str) and actual is not None:
        actual = str(actual)
    if comparison in (CASELESS, DOMAIN) and isinstance(actual, str) and isinstance(expected, str):
        actual, expected = actual.casefold(), expected.casefold()
        if comparison == DOMAIN:
            return actual == expected or actual.endswith("." + expected)
    return actual == expected


def test_matches(test: Mapping[str, Any], facts: Mapping[str, Any], how: str = EXACT) -> bool:
    actual = facts.get(test["field"])
    op = test["op"]
    if op == "present":
        return actual is not None
    value = test.get("value")
    if op == "eq":
        return actual is not None and _same(actual, value, how)
    if op == "not_eq":
        return not _same(actual, value, how)
    if op == "in":
        return actual is not None and any(_same(actual, item, how) for item in value or ())
    if op == "prefix":
        if not (isinstance(actual, str) and isinstance(value, str)):
            return False
        return actual.casefold().startswith(value.casefold()) if how != EXACT else actual.startswith(value)
    return False


def comparisons(connector: str) -> dict[str, str]:
    """How each of a connector's facts compares when not exactly."""
    manifest = _manifest(connector)
    if manifest is None:
        return {}
    declared = dict(manifest.fact_comparison)
    if manifest.repository_fact:
        declared[manifest.repository_fact] = CASELESS
    return declared


def matches(rule: Rule, facts: Mapping[str, Any]) -> bool:
    how = comparisons(rule.connector)
    return all(test_matches(test, facts, how.get(test["field"], EXACT))
               for test in rule.match)
