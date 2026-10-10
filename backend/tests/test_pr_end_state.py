"""The GitHub PR terminal rule has exactly one owner: ``pr_end_state``.

"A closed pull request ended merged when ``pull_request.merged`` is True,
closed-unmerged when it is False, unclassified otherwise." One table pins that
sentence at every consumer call site — the helper itself, the webhook mapper, the
connector classifier and the router's terminal write — so the rule is one edit
wherever it changes, and a payload that is not a classified close never becomes a
silently dropped terminal write (#2381 review, EAS2).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from events.connectors.github import MANIFEST, PR_MERGED_KEY, pr_end_state
from events.github_mapper import map_github_event, pr_terminal_state
from events.identity import PR_TYPE
from events.identity_formula import MalformedEventError

_REPO_FULL_NAME = "o/r"
_PR_NUMBER = 7
_PR_TITLE = "One owner for the rule"
_EXTERNAL_ID = f"{_REPO_FULL_NAME}#{_PR_NUMBER}"
_MISSING = object()

# PrEnd written into each consumer's own vocabulary. These rows are names only:
# every rule they test reads `merged` in exactly one place, ``pr_end_state``.
_STATE_BY_MERGED: dict[Any, str] = {True: "merged", False: "closed"}
WEBHOOK_EVENT = {"merged": "webhook.github.pr_merged", "closed": "webhook.github.pr_closed"}
MANIFEST_TYPE = {"merged": "pr.merged", "closed": "pr.closed"}
TERMINAL_EVENTS = sorted(WEBHOOK_EVENT.values())
OPENED_EVENTS = {"opened": "webhook.github.pr_opened", "synchronize": "webhook.github.pr_synchronize"}
# What `_classify` names a payload that is not a close: the action it carries,
# or the fall-through name when it carries none.
CLASSIFY_BY_ACTION = {"opened": "pr.opened", "synchronize": "pr.synchronize", "": "pr.opened"}


def _close(merged: Any = _MISSING, *, action: str = "closed") -> dict[str, Any]:
    """One ``pull_request`` webhook payload, in the shape GitHub sends today.

    ``merged`` defaults to absent, so a row says what it means about the key's
    presence and its value; ``_MISSING`` is the payload GitHub sends when the
    field is not there at all.
    """
    pr: dict[str, Any] = {"number": _PR_NUMBER, "title": _PR_TITLE}
    if merged is not _MISSING:
        pr[PR_MERGED_KEY] = merged
    return {"repository": {"full_name": _REPO_FULL_NAME}, "action": action, "pull_request": pr}


# (case, payload, the terminal state the payload gives — None gives no close)
ROWS: list[tuple[str, dict[str, Any], str | None]] = [
    ("true", _close(True), "merged"),
    ("false", _close(False), "closed"),
    ("missing", _close(), None),
    ("null", _close(None), None),
    ("zero", _close(0), None),
    ("empty_string", _close(""), None),
    ("non_bool", _close("true"), None),
    ("merged_is_dict", _close({}), None),
    ("merged_is_list", _close([]), None),
    ("opened", _close(True, action="opened"), None),
    ("synchronize", _close(True, action="synchronize"), None),
    ("no_pull_request", {"repository": {"full_name": _REPO_FULL_NAME}, "action": "closed"}, None),
    ("pull_request_null", _close(True) | {"pull_request": None}, None),
    # A body can hold any JSON value where GitHub puts an object. The rule reads
    # no fact out of that; it must not raise out of it either.
    ("pull_request_list", _close(True) | {"pull_request": ["merged"]}, None),
    ("pull_request_str", _close(True) | {"pull_request": "merged"}, None),
    ("pull_request_int", _close(True) | {"pull_request": 1}, None),
    ("pull_request_bool", _close(True) | {"pull_request": True}, None),
    ("repository_list", {**_close(True), "repository": ["o/r"]}, "merged"),
    ("action_missing", {"pull_request": {"merged": True}}, None),
]
IDS = [case for case, _, _ in ROWS]
BY_CASE = {case: (payload, state) for case, payload, state in ROWS}
UNCLASSIFIED_CLOSES = [
    case for case, (payload, state) in BY_CASE.items() if state is None and payload.get("action") == "closed"
]


class TestHelperIsTheRule:
    """The whole sentence, in one place, for one payload."""

    @pytest.mark.parametrize(("case", "payload", "state"), ROWS, ids=IDS)
    def test_two_booleans_classify_and_nothing_else_does(self, case, payload, state):
        assert pr_end_state(payload) == state, case

    @pytest.mark.parametrize(
        "bad", ["merged", 1, True, 3.5, ["merged"], {1, 2}, None, object()]
    )
    def test_a_body_holding_anything_where_an_object_goes_never_raises(self, bad):
        """A malformed delivery is an unclassified close, not a failed consumer."""
        assert pr_end_state({"action": "closed", "pull_request": bad}) is None


class TestFalsifiable:
    """Each guard does work: take it away and the table changes its answer."""

    @pytest.mark.parametrize("case", ["zero", "empty_string", "non_bool", "merged_is_dict", "merged_is_list"])
    def test_a_truthiness_check_would_admit_values_the_rule_refuses(self, case):
        """0, "", [] and "true" are not booleans: identity is the only guard that refuses them."""
        payload, state = BY_CASE[case]
        assert pr_end_state(payload) is state

        def _by_truthiness(one: dict[str, Any]) -> str | None:
            merged = (one.get("pull_request") or {}).get(PR_MERGED_KEY)
            if merged:
                return "merged"
            return "closed" if merged is not None else None

        assert _by_truthiness(payload) is not state

    @pytest.mark.parametrize("case", ["opened", "synchronize", "action_missing"])
    def test_dropping_the_action_check_would_call_a_non_close_terminal(self, case):
        """An unread key defaults to None, which is a state the rule must refuse."""
        payload, state = BY_CASE[case]
        assert pr_end_state(payload) is state

        def _without_the_action_check(one: dict[str, Any]) -> str | None:
            inner = one.get("pull_request") or {}
            return _STATE_BY_MERGED.get(inner.get(PR_MERGED_KEY))

        assert _without_the_action_check(payload) is not state

    def test_a_state_string_check_would_admit_values_the_rule_refuses(self):
        """The rule keys on the boolean, never on a word appearing in the payload."""

        def _by_state_string(one: dict[str, Any]) -> str | None:
            blob = str(one)
            if "merged" in blob:
                return "merged"
            return "closed" if "closed" in blob else None

        for case in ("non_bool", "null", "zero", "empty_string", "merged_is_dict"):
            payload, state = BY_CASE[case]
            assert _by_state_string(payload) is not state, case
        assert pr_end_state(_close("merged")) is None


class TestWebhookMapper:
    """`map_github_event` names the helper's state, or names nothing at all."""

    @pytest.mark.parametrize(("case", "payload", "state"), ROWS, ids=IDS)
    def test_mapper_names_exactly_the_helper_state(self, case, payload, state):
        if state is None and payload.get("action") != "closed":
            # Not a close at all: the mapper's own branch, untouched by the rule.
            assert map_github_event("pull_request", payload) == OPENED_EVENTS.get(
                str(payload.get("action"))
            ), case
            return
        expected = None if state is None else WEBHOOK_EVENT[state]
        assert map_github_event("pull_request", payload) == expected, case


class TestConnectorClassifier:
    """`_classify` maps the same rule into manifest types and fails closed."""

    @pytest.mark.parametrize(("case", "payload", "state"), ROWS, ids=IDS)
    def test_classifier_maps_exactly_the_helper_state(self, case, payload, state):
        if state is None:
            if payload.get("action") == "closed":
                with pytest.raises(MalformedEventError):
                    MANIFEST.classify(payload)
            else:
                assert MANIFEST.classify(payload) == CLASSIFY_BY_ACTION[payload.get("action", "")], case
        else:
            assert MANIFEST.classify(payload) == MANIFEST_TYPE[state], case

    @pytest.mark.parametrize("case", UNCLASSIFIED_CLOSES)
    def test_a_close_we_cannot_read_raises_instead_of_guessing(self, case):
        """Agent Substrate's binding amendment: an unclassified close has no class."""
        payload, _state = BY_CASE[case]
        with pytest.raises(MalformedEventError):
            MANIFEST.classify(payload)

    @pytest.mark.parametrize("bad", ["merged", 1, ["merged"], {1, 2}])
    def test_a_malformed_pull_request_refuses_as_malformed_not_as_a_crash(self, bad):
        """The classifier's own message reads the object too: it must not raise first."""
        with pytest.raises(MalformedEventError):
            MANIFEST.classify({"action": "closed", "pull_request": bad})

    def test_the_rejection_names_the_shape_it_could_not_read(self):
        """An operator reading the log must see what GitHub did not send."""
        with pytest.raises(MalformedEventError) as exc:
            MANIFEST.classify({"action": "closed", "pull_request": ["merged"],
                               "repository": {"full_name": _REPO_FULL_NAME}})
        assert "NoneType" in str(exc.value) and _REPO_FULL_NAME in str(exc.value)

    def test_the_raise_reaches_normalization_as_its_loud_rejection(self):
        """``MalformedEventError`` is the rejection ``normalize`` already documents."""
        from events.envelope import normalize

        with pytest.raises(MalformedEventError):
            normalize("github", BY_CASE["null"][0])
        with pytest.raises(MalformedEventError):
            normalize("github", {"action": "closed", "pull_request": ["merged"]})

    @pytest.mark.parametrize(("case", "payload", "state"), ROWS, ids=IDS)
    def test_a_classified_close_is_the_declared_informational_event(self, case, payload, state):
        """The class comes from the same rule, never a second reading of `merged`."""
        from events.connectors.github import github_event_class

        if state is None:
            pytest.skip("an unclassified close reaches no declaration")
        assert github_event_class(payload) == "informational", case


class _Identity:
    integration, type, external_id = "github", PR_TYPE, _EXTERNAL_ID


class TestRouterTerminalWrite:
    """The router writes only what the rule confirms, and refuses out loud."""

    @pytest.fixture(autouse=True)
    def _restore_router_writers(self, monkeypatch):
        from events import router

        writes: list[Any] = []
        self.writes = writes
        monkeypatch.setattr(
            router,
            "record_pull_request_ended",
            lambda integration, type_, external_id, *, pr_state, **kw: writes.append(
                (external_id, pr_state)
            ),
        )
        monkeypatch.setattr(
            router, "schedule_merged_pr_reap", lambda _payload: writes.append(("reap",))
        )

    def _record(self, event: str, payload: dict[str, Any]) -> list[Any]:
        from events import router

        router._record_source_facts(event, _Identity, payload)
        return self.writes

    @pytest.mark.parametrize("event", TERMINAL_EVENTS)
    @pytest.mark.parametrize(("case", "payload", "state"), ROWS, ids=IDS)
    def test_the_write_is_the_helper_state_or_nothing_at_all(self, event, case, payload, state):
        """One payload, both terminal event names: agree and write, else refuse."""
        self._record(event, payload)
        if state is None or WEBHOOK_EVENT[state] != event:
            assert self.writes == [], f"{case} as {event} must not write"
            return
        expected = [(_EXTERNAL_ID, state)]
        if state == "merged":
            expected.append(("reap",))
        assert self.writes == expected, f"{case} as {event}"

    @pytest.mark.parametrize(("case", "payload", "state"), ROWS, ids=IDS)
    def test_the_mapper_and_the_write_can_never_disagree(self, case, payload, state):
        """What the mapper names is exactly what the router is entitled to write."""
        named = map_github_event("pull_request", payload)
        assert pr_terminal_state(named, payload, _EXTERNAL_ID) == state, case
        if named is None:
            assert self._record(str(named), payload) == [], case

    @pytest.mark.parametrize("case", UNCLASSIFIED_CLOSES)
    def test_an_unclassified_close_writes_nothing(self, case):
        payload, _state = BY_CASE[case]
        assert self._record(WEBHOOK_EVENT["merged"], payload) == []

    @pytest.mark.parametrize(
        ("event", "merged", "claims", "gives"),
        [
            (WEBHOOK_EVENT["merged"], False, "merged", "closed"),
            (WEBHOOK_EVENT["closed"], True, "closed", "merged"),
            (WEBHOOK_EVENT["merged"], _MISSING, "merged", "None"),
            (WEBHOOK_EVENT["merged"], None, "merged", "None"),
            (WEBHOOK_EVENT["merged"], "true", "merged", "None"),
        ],
    )
    def test_refusal_warns_with_the_event_the_pr_and_both_values(
        self, caplog, event, merged, claims, gives
    ):
        """The signal #2381 never had: a refused write is named, never silent."""
        with caplog.at_level(logging.WARNING, logger="events.github_mapper"):
            assert self._record(event, _close(merged)) == []

        text = caplog.text
        assert "refusing the terminal PR write" in text, event
        assert _EXTERNAL_ID in text and event in text, event
        assert f"claims {claims}" in text and f"gives {gives}" in text, event
        assert _PR_TITLE not in text, "the warning must not dump the payload"

    @pytest.mark.parametrize("event", ["webhook.github.pr_opened", "webhook.github.pr_comment"])
    def test_a_non_terminal_event_writes_nothing_and_stays_quiet(self, caplog, event):
        with caplog.at_level(logging.WARNING, logger="events.github_mapper"):
            assert self._record(event, _close(True, action="opened")) == []
        assert caplog.text == ""
