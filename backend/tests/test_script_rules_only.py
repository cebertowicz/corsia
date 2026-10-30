"""B3: a script with no next run is the one mark that only event rules start it; ``spec`` no
longer carries it, so a spec that disagrees can't flip what ``automation_list`` says."""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_the_missing_next_run_alone_says_rules_only_whatever_the_spec(isolated_db, tmp_path, monkeypatch):
    from infra.database import get_db, write_transaction
    from tools import automations

    with write_transaction(None, scope="rules_only_owner") as db:
        db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('owner','user','t','t')")
    path = tmp_path / "watch.py"
    path.write_text("print('x')")
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *_a, **_k: True)
    actor = {"project_id": None}
    timed = automations._operate("owner", actor, "schedule", {
        "kind": "script", "script_path": str(path), "cadence_seconds": 600,
        "series": [{"key": "cost", "unit": "usd", "max": 5, "worse": "up"}]})["schedule"]
    rules_only = automations._operate("owner", actor, "schedule",
                                      {"kind": "script", "script_path": str(path)})["schedule"]
    assert (rules_only["spec"], rules_only["next_run_at"]) == ("", None)

    # The disagreeing pair: a timed script whose spec says 'rules', a rules-only one with a cadence spec.
    with write_transaction(None, scope="rules_only_disagree") as db:
        db.execute("UPDATE automation_schedules SET spec='rules' WHERE id=?", (timed["id"],))
        db.execute("UPDATE automation_schedules SET spec='600' WHERE id=?", (rules_only["id"],))
    shown = {item["id"]: item for item in automations._operate("owner", actor, "list", {})["schedules"]}
    assert "runs_from" not in shown[timed["id"]]
    assert shown[rules_only["id"]]["runs_from"].startswith("event rules only")
    assert get_db().execute("SELECT COUNT(*) FROM automation_schedules WHERE spec='rules' "
                            "AND next_run_at IS NULL").fetchone()[0] == 0
