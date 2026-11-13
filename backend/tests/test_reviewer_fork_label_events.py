"""The review label maps to its own event and never wakes the owner."""

from __future__ import annotations

from reviewer.reviewer_categories import LABEL_REVIEW


def _labeled(name: str) -> dict:
    return {
        "action": "labeled",
        "label": {"name": name},
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }


def test_the_review_label_maps_to_its_event() -> None:
    from events.github_mapper import map_github_event

    assert map_github_event("pull_request", _labeled(LABEL_REVIEW)) == "webhook.github.pr_labeled"


def test_removing_the_label_maps_to_its_own_event() -> None:
    from events.github_mapper import map_github_event

    event = map_github_event("pull_request", {**_labeled(LABEL_REVIEW), "action": "unlabeled"})
    assert event == "webhook.github.pr_unlabeled"


def test_the_label_event_does_not_wake_the_owner() -> None:
    from events.connectors.github import MANIFEST
    from events.disposition import disposition
    from events.identity import resolve_identity

    payload = _labeled(LABEL_REVIEW)
    payload["pull_request"]["head"]["repo"] = {"full_name": "fork/r"}
    event_type = MANIFEST.classify(payload)

    assert event_type == "pr.labeled"
    assert disposition("owner", MANIFEST.decl(event_type).event_class) == "digest"
    assert resolve_identity("webhook.github.pr_labeled", payload).external_id == "o/r#7"
