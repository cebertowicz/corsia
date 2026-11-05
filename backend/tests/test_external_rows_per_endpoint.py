"""Two External endpoints may serve the same model id.

A vendor's pay-as-you-go key and its plan key reach the same models through
different endpoints. Every External row shares ``provider='external'`` and the
catalog is unique on ``(provider, provider_model_id)``, so the second endpoint's
discovery used to fail on that constraint and its models never appeared.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

_CAPS = {"supports_vision": 0, "supports_tools": 1, "supports_reasoning": 1}


def _seed_servers(path, *ids: str) -> None:
    with closing(sqlite3.connect(str(path))) as db:
        for sid in ids:
            db.execute(
                "INSERT INTO network_servers (id,provider,kind,dialect,instance_id,"
                "instance_name,port,status,last_seen_at,created_at,updated_at) "
                "VALUES (?,'external','external','openai',?,?,443,'online','t0','t0','t0')",
                (sid, f"{sid}.example.com:443/v1", sid),
            )
        db.commit()


def _reconcile(sid: str, model_ids: list[str]) -> dict:
    from llm import external_provider as ep

    server = {"id": sid, "instance_name": sid, "status": "online"}
    result = {"ok": False, "discovered": 0, "updated": 0, "missing": 0}
    return ep._reconcile_external_models(
        sid, server, "openai", model_ids, _CAPS, "t1", result,
    )


def _rows(path) -> list[tuple]:
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(
            "SELECT provider_model_id, litellm_model, available FROM models "
            "WHERE provider = 'external' ORDER BY litellm_model"
        ).fetchall()


def test_two_endpoints_serving_one_model_each_get_their_own_row(isolated_db) -> None:
    _seed_servers(isolated_db, "ns-paygo", "ns-plan")

    assert _reconcile("ns-paygo", ["glm-5.3"])["ok"] is True
    assert _reconcile("ns-plan", ["glm-5.3", "deepseek-v4.1-flash"])["ok"] is True

    assert _rows(isolated_db) == [
        ("ns-paygo/glm-5.3", "external@ns-paygo/glm-5.3", 1),
        ("ns-plan/deepseek-v4.1-flash", "external@ns-plan/deepseek-v4.1-flash", 1),
        ("ns-plan/glm-5.3", "external@ns-plan/glm-5.3", 1),
    ]


def test_a_row_written_with_the_bare_id_is_adopted_not_duplicated(isolated_db) -> None:
    _seed_servers(isolated_db, "ns-paygo", "ns-plan")
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO models (id,provider,provider_model_id,litellm_model,display_name,"
            "source,enabled,available,context_window,created_at,updated_at) VALUES "
            "('ext-old','external','glm-5.3','external@ns-paygo/glm-5.3','glm-5.3',"
            "'discovered',1,1,262144,'t0','t0')"
        )
        db.commit()

    assert _reconcile("ns-paygo", ["glm-5.3"])["updated"] == 1
    assert _reconcile("ns-plan", ["glm-5.3"])["discovered"] == 1

    with closing(sqlite3.connect(str(isolated_db))) as db:
        kept = db.execute(
            "SELECT provider_model_id, context_window FROM models WHERE id = 'ext-old'"
        ).fetchone()
    assert kept == ("ns-paygo/glm-5.3", 262144)
    assert len(_rows(isolated_db)) == 2


def test_a_model_one_endpoint_stops_serving_leaves_the_others_row(isolated_db) -> None:
    _seed_servers(isolated_db, "ns-paygo", "ns-plan")
    _reconcile("ns-paygo", ["glm-5.3"])
    _reconcile("ns-plan", ["glm-5.3"])

    assert _reconcile("ns-plan", [])["missing"] == 1

    assert _rows(isolated_db) == [("ns-paygo/glm-5.3", "external@ns-paygo/glm-5.3", 1)]


def test_the_provider_card_lists_and_toggles_the_served_ids(isolated_db) -> None:
    from llm import catalog
    from llm.external_catalog_view import external_catalog_models

    _seed_servers(isolated_db, "ns-plan")
    _reconcile("ns-plan", ["qwen/qwen3-coder", "glm-5.3"])

    listed = external_catalog_models("ns-plan")["loaded"]
    assert [m["model_id"] for m in listed] == ["glm-5.3", "qwen/qwen3-coder"]
    db_id = next(m["db_id"] for m in listed if m["model_id"] == "qwen/qwen3-coder")
    assert catalog.update_model(db_id, {"enabled": 0}) is True
    assert [m["model_id"] for m in external_catalog_models("ns-plan")["available"]] == [
        "qwen/qwen3-coder",
    ]


def test_the_provider_card_shows_the_saved_checkpoint_window(isolated_db) -> None:
    from llm.external_catalog_view import external_catalog_models

    _seed_servers(isolated_db, "ns-plan")
    _reconcile("ns-plan", ["glm-5.3", "deepseek-v4.1-flash"])
    saved_id = next(
        m["db_id"] for m in external_catalog_models("ns-plan")["loaded"] if m["model_id"] == "glm-5.3"
    )
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO model_profiles (model_id, context_window) VALUES (?,?)", (saved_id, 262144)
        )
        db.commit()

    listed = {m["model_id"]: m for m in external_catalog_models("ns-plan")["loaded"]}
    assert listed["glm-5.3"]["context_window_override"] == 262144
    assert listed["deepseek-v4.1-flash"]["context_window_override"] is None


def _windows(path) -> dict[str, int | None]:
    with closing(sqlite3.connect(str(path))) as db:
        return dict(db.execute(
            "SELECT provider_model_id, context_window FROM models WHERE provider = 'external'"
        ).fetchall())


def test_a_window_the_server_reports_is_kept_and_the_users_window_wins(isolated_db) -> None:
    from llm import external_provider as ep

    _seed_servers(isolated_db, "ns-vllm", "ns-user")
    server = {"id": "ns-vllm", "instance_name": "ns-vllm", "status": "online"}
    reported = {"served": 32768, "unreported": None}
    ep._reconcile_external_models(
        "ns-vllm", server, "openai", list(reported), _CAPS, "t1",
        {"ok": False, "discovered": 0, "updated": 0, "missing": 0},
        reported_windows=reported,
    )
    ep._reconcile_external_models(
        "ns-user", {**server, "id": "ns-user"}, "openai", ["served"],
        {**_CAPS, "context_window": 8192}, "t1",
        {"ok": False, "discovered": 0, "updated": 0, "missing": 0},
        reported_windows={"served": 32768},
    )

    assert _windows(isolated_db) == {
        "ns-vllm/served": 32768, "ns-vllm/unreported": None, "ns-user/served": 8192,
    }
