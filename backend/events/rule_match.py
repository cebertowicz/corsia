"""Whether an event's facts pass a rule's tests.

A test on the connector's repository fact compares without case, the same as
``create`` validates it against the project's repositories: a webhook keeps
the owner's and repository's casing, and the project's remotes are lowercase.
Names no connector.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from events.rules import Rule


def repository_fact(connector: str) -> str | None:
    """The fact naming an event's repository, if the connector declares one."""
    from events.connectors import get_manifest

    try:
        return get_manifest(connector).repository_fact
    except KeyError:
        return None


def _same(actual: Any, expected: Any, fold: bool) -> bool:
    if isinstance(expected, str) and not isinstance(actual, str) and actual is not None:
        actual = str(actual)
    if fold and isinstance(actual, str) and isinstance(expected, str):
        return actual.casefold() == expected.casefold()
    return actual == expected


def test_matches(test: Mapping[str, Any], facts: Mapping[str, Any], fold: bool = False) -> bool:
    actual = facts.get(test["field"])
    op = test["op"]
    if op == "present":
        return actual is not None
    value = test.get("value")
    if op == "eq":
        return actual is not None and _same(actual, value, fold)
    if op == "not_eq":
        return not _same(actual, value, fold)
    if op == "in":
        return actual is not None and any(_same(actual, item, fold) for item in value or ())
    if op == "prefix":
        if not (isinstance(actual, str) and isinstance(value, str)):
            return False
        return actual.casefold().startswith(value.casefold()) if fold else actual.startswith(value)
    return False


def matches(rule: Rule, facts: Mapping[str, Any]) -> bool:
    fact = repository_fact(rule.connector)
    return all(test_matches(test, facts, fold=test["field"] == fact) for test in rule.match)
