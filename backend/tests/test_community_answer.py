"""Answering the community decision writes the person's two profile lines.

The decision's answer is the consent that turns the skill library on, so the
answer path records it in ``context/50-USER-PROFILE.md`` itself.
"""

from __future__ import annotations

import json

import pytest

from infra import config, user_profile
from infra.corsia_agent import SETTINGS_KEY
from onboarding import setup_decisions, setup_strings
from tests.test_attention_queue import db, raise_decision  # noqa: F401 - shared fixtures
from tests.test_decision_lifecycle import _client

PROFILE = user_profile.PROFILE_RELATIVE_PATH
NAME_LINES = "# User profile\n\n- **Full name:** Ada Lovelace\n- **Preferred name:** Ada\n"
EXPECTED = {
    "A": "- **Use community improvements:** Yes\n- **Share improvements with the community:** Yes\n",
    "B": "- **Use community improvements:** Yes\n- **Share improvements with the community:** No\n",
    "C": "- **Use community improvements:** No\n- **Share improvements with the community:** No\n",
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    (tmp_path / PROFILE).parent.mkdir(parents=True)
    (tmp_path / PROFILE).write_text(NAME_LINES, encoding="utf-8")
    return tmp_path


def _community_card() -> dict:
    card = next(d for d in setup_decisions.build_decisions(setup_decisions.Facts())
                if d["title"] == setup_strings.COMMUNITY["title"])
    return {key: card[key] for key in
            ("title", "context", "tradeoff", "recommendation", "rationale", "options", "other_enabled")}


def _make_corsia_agent(db, thread_id: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
               (SETTINGS_KEY, json.dumps({"session_id": thread_id}), "t"))
    db.commit()


def _answer(db, monkeypatch, decision, option_id: str, other_text=None):
    body = {"option_id": option_id, "expected_revision": decision["revision"],
            "mutation_id": f"answer-{decision['id']}-{option_id}"}
    if other_text is not None:
        body["other_text"] = other_text
    return _client(db, monkeypatch).post(f"/api/plans/decisions/{decision['id']}/answer", json=body)


def _profile(workspace) -> str:
    return (workspace / PROFILE).read_text(encoding="utf-8")


def _raise(db, **card_overrides) -> dict:
    _make_corsia_agent(db, "child")
    return raise_decision(db, "child", "child", **{**_community_card(), **card_overrides})


@pytest.mark.parametrize("option_id", ["A", "B", "C"])
def test_each_option_writes_exactly_its_two_lines(db, monkeypatch, workspace, option_id):
    decision = _raise(db)

    assert _answer(db, monkeypatch, decision, option_id).status_code == 200

    assert _profile(workspace) == NAME_LINES + EXPECTED[option_id]
    assert user_profile.uses_community_improvements(workspace) is (option_id != "C")


def test_an_existing_line_is_replaced_not_duplicated(db, monkeypatch, workspace):
    (workspace / PROFILE).write_text(
        NAME_LINES + "- **Use community improvements:** No\n- **Share improvements with the community:** Yes\n"
        "- **Timezone:** America/Los_Angeles\n", encoding="utf-8")
    decision = _raise(db)

    assert _answer(db, monkeypatch, decision, "B").status_code == 200

    assert _profile(workspace) == NAME_LINES + EXPECTED["B"] + "- **Timezone:** America/Los_Angeles\n"


def test_a_missing_profile_is_created_with_the_two_lines(db, monkeypatch, workspace):
    (workspace / PROFILE).unlink()
    decision = _raise(db)

    assert _answer(db, monkeypatch, decision, "A").status_code == 200

    assert _profile(workspace) == "# User profile\n\n" + EXPECTED["A"]


def test_a_note_only_answer_writes_nothing(db, monkeypatch, workspace):
    decision = _raise(db)

    assert _answer(db, monkeypatch, decision, "other", other_text="not now").status_code == 200

    assert _profile(workspace) == NAME_LINES


def test_another_owner_writes_nothing(db, monkeypatch, workspace):
    _make_corsia_agent(db, "root")
    decision = raise_decision(db, "child", "child", **_community_card())

    assert _answer(db, monkeypatch, decision, "A").status_code == 200

    assert _profile(workspace) == NAME_LINES


def test_another_title_writes_nothing(db, monkeypatch, workspace):
    decision = _raise(db, title="Use and share something else?")

    assert _answer(db, monkeypatch, decision, "A").status_code == 200

    assert _profile(workspace) == NAME_LINES


def test_a_write_failure_leaves_the_answer_committed(db, monkeypatch, workspace):
    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(user_profile, "stage_community_answer", broken)
    decision = _raise(db)

    assert _answer(db, monkeypatch, decision, "A").status_code == 200

    assert _profile(workspace) == NAME_LINES
    state = db.execute(
        "SELECT metadata FROM artifact_identities WHERE integration='local' AND type='plan-decision' AND external_id=?",
        (decision["id"],)).fetchone()["metadata"]
    assert json.loads(state)["plan_decision"]["state"] == "answered"


def test_a_reworded_option_writes_nothing_and_warns(db, monkeypatch, workspace, caplog):
    options = [{**option, "label": option["label"] + " please"} for option in _community_card()["options"]]
    decision = _raise(db, options=options)

    assert _answer(db, monkeypatch, decision, "A").status_code == 200

    assert _profile(workspace) == NAME_LINES
    assert "option label CORSIA does not know" in caplog.text


def test_each_setup_label_maps_to_its_pair_by_its_text():
    from onboarding.community_answer import _ANSWERS

    labels = [label for label, _detail in setup_strings.COMMUNITY["options"]]

    assert sorted(labels) == sorted(_ANSWERS)
    assert _ANSWERS["Use and share"] == (True, True)
    assert _ANSWERS["Only use"] == (True, False)
    assert _ANSWERS["Neither"] == (False, False)
