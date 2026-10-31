
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CORSIA_WORKSPACE", "/tmp/corsia-test-ws")

from llm.context_window import (  # noqa: E402
    ModelContextWindowUnknown,
    get_context_window,
    resolve_window_facts,
)




def _patch_lookups(
    *,
    catalog_row: dict | None,
    profile_row: dict | None,
    profile_error: Exception | None = None,
):
    class _FakeCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return dict(self._row) if self._row is not None else None

    class _FakeDB:
        def __init__(self):
            pass

        def execute(self, sql, *_a, **_k):
            if "FROM model_profiles" in sql:
                if profile_error is not None:
                    raise profile_error
                return _FakeCursor(profile_row)
            if "FROM models" in sql:
                return _FakeCursor(catalog_row)
            return _FakeCursor(None)

        def close(self):
            pass

    db_patch = patch("infra.database.get_db", return_value=_FakeDB())
    if not catalog_row or catalog_row.get("harness") not in ("claude-code", "codex"):
        return nullcontext(), db_patch
    # A harness row's window is what a signed-in account's discovery lists for it now.
    catalog_row = {**catalog_row, "litellm_model": catalog_row["id"]}
    listed = {catalog_row["id"]: {
        "available": True, "context_window": catalog_row["context_window"], "supports_vision": 0,
        "supports_reasoning": 0, "display_name": catalog_row["id"], "description": "", "last_error": None,
        "metadata_json": "{}",
    }}
    return patch("llm.harness_catalog.account_rows", return_value=listed), db_patch




def test_returns_stored_window_from_catalog_for_local_model():
    # Plumbing test: the resolver returns the window the catalog row holds.
    # The model name is a synthetic label — the window is provider-reported
    # data CORSIA stores, not a value CORSIA owns.
    cat, db = _patch_lookups(
        catalog_row={"id": "vmlx-130a2a75", "context_window": 256000},
        profile_row=None,
    )
    with cat, db:
        assert (
            get_context_window("omlx@ns-abc/Local-Model-A3B-8bit") == 256000
        )


def test_returns_stored_window_from_catalog_for_second_local_model():
    cat, db = _patch_lookups(
        catalog_row={"id": "vmlx-second", "context_window": 262144},
        profile_row=None,
    )
    with cat, db:
        assert (
            get_context_window("omlx@ns-abc/Model-ABC-MLX-8bit") == 262144
        )


def test_returns_stored_window_from_catalog_for_cloud_model():
    # Nothing saved, so the CORSIA default caps the provider's 1M window.
    cat, db = _patch_lookups(
        catalog_row={"id": "anthropic-claude-opus-4-7", "context_window": 1_000_000},
        profile_row=None,
    )
    with cat, db:
        assert (
            get_context_window("anthropic/claude-opus-4-7") == 262_144
        )


@pytest.mark.parametrize(
    ("model", "discovered"),
    [("claude-code/test-exact-selector-a", 314_159),
     ("claude-code/test-exact-selector-b", 271_828)],
)
def test_claude_code_window_comes_from_exact_catalog_row(model, discovered):
    # The two selectors hold different windows, so a cross-row read shows up;
    # nothing is saved, so both now count against the CORSIA default.
    cat, db = _patch_lookups(
        catalog_row={
            "id": model, "harness": "claude-code", "context_window": discovered,
        },
        profile_row=None,
    )
    with cat, db:
        assert get_context_window(model) == 262_144
        assert resolve_window_facts(
            {"id": model, "harness": "claude-code", "context_window": discovered},
        ).discovered == discovered


def test_codex_window_comes_from_exact_catalog_row():
    cat, db = _patch_lookups(
        catalog_row={
            "id": "codex/gpt-5-codex", "harness": "codex",
            "context_window": 258400,
        },
        profile_row=None,
    )
    with cat, db:
        assert get_context_window("codex/gpt-5-codex") == 258400


@pytest.mark.parametrize(
    ("profile_window", "expected"),
    ((200_000, 200_000), (300_000, 258_400)),
)
def test_harness_profile_caps_but_never_expands_discovered_window(
    profile_window, expected
):
    cat, db = _patch_lookups(
        catalog_row={
            "id": "codex/gpt-5-codex", "harness": "codex",
            "context_window": 258_400,
        },
        profile_row={"context_window": profile_window},
    )
    with cat, db:
        assert get_context_window("codex/gpt-5-codex") == expected


@pytest.mark.parametrize("model", ["claude-code/opus", "codex/gpt-5-codex"])
def test_harness_without_discovered_window_uses_only_a_saved_window(model):
    row = {"id": model, "harness": model.split("/", 1)[0], "context_window": None}
    cat, db = _patch_lookups(catalog_row=row, profile_row={"context_window": 200000})
    with cat, db:
        assert get_context_window(model) == 200000
    cat, db = _patch_lookups(catalog_row=row, profile_row=None)
    with cat, db, pytest.raises(ModelContextWindowUnknown):
        get_context_window(model)


def test_native_profile_override_still_wins_over_catalog():
    cat, db = _patch_lookups(
        catalog_row={"id": "x", "context_window": 200_000},
        profile_row={"context_window": 400_000},
    )
    with cat, db:
        assert get_context_window("anthropic/x") == 400_000


def test_native_profile_override_does_not_require_discovered_window():
    cat, db = _patch_lookups(
        catalog_row={"id": "x", "context_window": None},
        profile_row={"context_window": 64_000},
    )
    with cat, db:
        assert get_context_window("anthropic/x") == 64_000


def test_unknown_model_raises_loudly():
    cat, db = _patch_lookups(catalog_row=None, profile_row=None)
    with cat, db:
        with pytest.raises(ModelContextWindowUnknown) as exc_info:
            get_context_window("unknown/model-id")
        assert "unknown/model-id" in str(exc_info.value)


def test_catalog_row_with_null_context_window_raises_loudly():
    cat, db = _patch_lookups(
        catalog_row={"id": "vmlx-bug", "context_window": None},
        profile_row=None,
    )
    with cat, db:
        with pytest.raises(ModelContextWindowUnknown):
            get_context_window("omlx@ns-abc/regressed-model")


def test_catalog_row_with_zero_context_window_raises_loudly():
    cat, db = _patch_lookups(
        catalog_row={"id": "vmlx-bug", "context_window": 0},
        profile_row=None,
    )
    with cat, db:
        with pytest.raises(ModelContextWindowUnknown):
            get_context_window("omlx@ns-abc/zero-window")


def test_profile_db_failure_falls_through_to_catalog():
    catalog_patch, db_patch = _patch_lookups(
        catalog_row={"id": "y", "context_window": 200000},
        profile_row=None,
        profile_error=RuntimeError("simulated profile lookup failure"),
    )
    with catalog_patch, db_patch:
        assert get_context_window("y") == 200000


def test_profile_with_zero_value_treated_as_no_override():
    cat, db = _patch_lookups(
        catalog_row={"id": "z", "context_window": 200000},
        profile_row={"context_window": 0},
    )
    with cat, db:
        assert get_context_window("z") == 200000


def test_profile_with_negative_value_treated_as_no_override():
    cat, db = _patch_lookups(
        catalog_row={
            "id": "codex/gpt-5-codex",
            "harness": "codex",
            "context_window": 200000,
        },
        profile_row={"context_window": -1},
    )
    with cat, db:
        assert get_context_window("codex/gpt-5-codex") == 200000


@pytest.mark.parametrize("legacy_value", (1.5, True, "100000"))
def test_malformed_legacy_profile_value_is_not_coerced(legacy_value):
    cat, db = _patch_lookups(
        catalog_row={
            "id": "codex/gpt-5-codex",
            "harness": "codex",
            "context_window": 200000,
        },
        profile_row={"context_window": legacy_value},
    )
    with cat, db, pytest.raises(ValueError, match="invalid persisted"):
        get_context_window("codex/gpt-5-codex")


def test_harness_profile_lookup_failure_does_not_bypass_cap():
    cat, db = _patch_lookups(
        catalog_row={
            "id": "codex/gpt-5-codex",
            "harness": "codex",
            "context_window": 258400,
        },
        profile_row=None,
        profile_error=RuntimeError("simulated profile lookup failure"),
    )
    with cat, db, pytest.raises(RuntimeError, match="simulated profile lookup failure"):
        get_context_window("codex/gpt-5-codex")


def test_never_returns_128000_silently_when_db_broken():
    cat, db = _patch_lookups(catalog_row=None, profile_row=None)
    with cat, db:
        try:
            result = get_context_window("does-not-exist")
        except ModelContextWindowUnknown:
            return  # correct
        pytest.fail(
            f"Resolver silently returned {result} for a model with no "
            f"DB row — anti-pattern #4 reintroduced. Must raise "
            f"ModelContextWindowUnknown instead."
        )


def test_never_returns_any_hardcoded_sentinel_when_db_broken():
    cat, db = _patch_lookups(catalog_row=None, profile_row=None)
    with cat, db:
        try:
            result = get_context_window("does-not-exist")
        except ModelContextWindowUnknown:
            return  # correct
        pytest.fail(
            f"Resolver returned hardcoded sentinel {result} instead of "
            f"raising — anti-pattern #4. Must raise "
            f"ModelContextWindowUnknown when DB has no value."
        )


def test_window_facts_expose_the_cap_and_the_provider_capacity():
    # #1753: an external harness keeps its own conversation, so its real
    # occupancy is bounded by the DISCOVERED window, not by CORSIA's cap. A
    # surface that sees only the cap cannot explain an over-window number, so
    # both travel together and the cap is marked as one.
    cat, db = _patch_lookups(
        catalog_row={
            "id": "claude-code/opus[1m]", "harness": "claude-code",
            "context_window": 1_000_000,
        },
        profile_row={"context_window": 262_144},
    )
    row = {
        "id": "claude-code/opus[1m]", "harness": "claude-code",
        "context_window": 1_000_000,
    }
    with cat, db:
        facts = resolve_window_facts(row)
    assert facts.effective == 262_144
    assert facts.discovered == 1_000_000


def test_window_facts_report_no_cap_when_no_profile_narrows_the_row():
    cat, db = _patch_lookups(
        catalog_row={
            "id": "codex/gpt-5-codex", "harness": "codex",
            "context_window": 258_400,
        },
        profile_row=None,
    )
    row = {
        "id": "codex/gpt-5-codex", "harness": "codex",
        "context_window": 258_400,
    }
    with cat, db:
        facts = resolve_window_facts(row)
    assert facts.effective == facts.discovered == 258_400


def test_native_override_raises_the_window_and_is_never_reported_as_a_cap():
    # Native honors an operator override outright rather than capping it, and
    # CORSIA builds the native prompt against that window, so this is not the
    # budget-versus-window state and must not be labelled as one.
    cat, db = _patch_lookups(
        catalog_row={
            "id": "local/qwen", "harness": "native", "context_window": 128_000,
        },
        profile_row={"context_window": 256_000},
    )
    row = {"id": "local/qwen", "harness": "native", "context_window": 128_000}
    with cat, db:
        facts = resolve_window_facts(row)
    assert facts.effective == 256_000
    assert facts.discovered == 128_000


@pytest.mark.parametrize("stored", (None, 0, -5, True, "abc"))
def test_a_malformed_stored_window_is_no_discovered_window(stored):
    from llm import context_window as cw

    row = {"id": "codex/x", "harness": "codex", "context_window": stored}
    with patch.object(cw, "_profile_context_window", return_value=None):
        assert cw.resolve_window_facts(row).discovered is None


@pytest.mark.parametrize("harness", ("claude-code", "codex"))
def test_a_saved_window_stands_when_the_cli_reports_none(harness):
    # A model the CLI lists without a window is unusable until the person saves
    # one; that saved value is then the only fact and is used as saved.
    from llm import context_window as cw

    row = {"id": f"{harness}/x", "harness": harness, "context_window": None}
    with patch.object(cw, "_profile_context_window", return_value=200_000):
        facts = cw.resolve_window_facts(row)
    assert (facts.effective, facts.discovered) == (200_000, None)
    with patch.object(cw, "_profile_context_window", return_value=None):
        assert cw.resolve_window_facts(row).effective is None


def test_a_codex_target_carries_both_windows_from_one_resolution():
    # A Codex spawn takes both windows from the resolved target, as Claude Code
    # does, so a capped budget and the provider capacity cannot disagree (#1868).
    from llm import context_window as cw

    row = {"id": "codex/x", "harness": "codex", "context_window": 400_000}
    with patch.object(cw, "_profile_context_window", return_value=262_144):
        facts = cw.resolve_window_facts(row)
    assert (facts.effective, facts.discovered) == (262_144, 400_000)


def _profiles_db(profiles: dict | None):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    if profiles is not None:
        db.execute("CREATE TABLE model_profiles (model_id TEXT PRIMARY KEY, context_window)")
        db.executemany("INSERT INTO model_profiles VALUES (?, ?)", profiles.items())
    return db


_ROWS = [
    {"id": "claude-code/sonnet", "harness": "claude-code", "context_window": 200_000},
    {"id": "codex/gpt-5.6-sol", "harness": "codex", "context_window": 400_000},
    {"id": "native-override", "harness": "native", "context_window": 128_000},
    {"id": "native-invalid", "harness": "native", "context_window": 64_000},
    {"id": "native-plain", "harness": "native", "context_window": 32_000},
    {"id": "other-harness", "harness": "other", "context_window": 16_000},
    {"harness": "native", "context_window": 8_000},
]


@pytest.mark.parametrize(
    "profiles",
    [
        {"claude-code/sonnet": 100_000, "native-override": 1_000_000, "native-invalid": "abc"},
        {},
        None,
    ],
    ids=["profiles", "no-profiles", "no-table"],
)
def test_batch_window_facts_match_the_single_row_resolver(profiles):
    from llm.context_window import resolve_window_facts_for_rows

    db = _profiles_db(profiles)
    single = [resolve_window_facts(row, db=db) for row in _ROWS]
    assert resolve_window_facts_for_rows(_ROWS, db=db) == single


def test_batch_and_single_row_treat_a_profile_read_failure_alike():
    import sqlite3

    from llm.context_window import resolve_window_facts_for_rows

    class _LockedProfiles:
        def execute(self, sql, params=()):
            if "model_profiles" in sql:
                raise sqlite3.OperationalError("database is locked")
            raise AssertionError(sql)

    db = _LockedProfiles()
    native = [row for row in _ROWS if row["harness"] != "claude-code" and row["harness"] != "codex"]
    assert resolve_window_facts_for_rows(native, db=db) == [
        resolve_window_facts(row, db=db) for row in native
    ]
    for harness_row in _ROWS[:2]:
        with pytest.raises(sqlite3.OperationalError):
            resolve_window_facts(harness_row, db=db)
        with pytest.raises(sqlite3.OperationalError):
            resolve_window_facts_for_rows([harness_row], db=db)


def test_batch_and_single_row_both_reject_an_invalid_harness_profile():
    from llm.context_window import resolve_window_facts_for_rows

    db = _profiles_db({"claude-code/sonnet": "abc"})
    with pytest.raises(ValueError):
        resolve_window_facts(_ROWS[0], db=db)
    with pytest.raises(ValueError):
        resolve_window_facts_for_rows([_ROWS[0]], db=db)


def test_external_listing_reports_the_windows_dispatch_would_use(isolated_db):
    from infra.database import get_db
    from llm.external_catalog_view import external_catalog_models

    db = get_db()
    try:
        db.executemany(
            """INSERT INTO models
               (id,harness,provider,provider_model_id,litellm_model,display_name,
                backend,source,enabled,available,context_window,created_at,updated_at)
               VALUES (?,'native','external',?,?,?,'api','discovered',1,1,?,'t','t')""",
            [
                ("ext-a", "saved", "external@srv/saved", "saved", 128_000),
                ("ext-b", "plain", "external@srv/plain", "plain", 64_000),
                ("ext-c", "unknown", "external@srv/unknown", "unknown", None),
            ],
        )
        db.execute(
            "INSERT OR REPLACE INTO model_profiles (model_id, context_window) VALUES (?, ?)",
            ("ext-a", 1_000_000),
        )
        db.commit()
        listed = {
            m["id"]: m for group in external_catalog_models("srv").values() for m in group
        }
        rows = {
            r["provider_model_id"]: r
            for r in db.execute("SELECT * FROM models WHERE provider = 'external'")
        }
        for pmid, model in listed.items():
            facts = resolve_window_facts(rows[pmid], db=db)
            assert (
                model["context_length"], model["provider_context_length"],
                model["context_window_source"],
            ) == facts
        assert listed["saved"]["context_length"] == 1_000_000
        assert listed["saved"]["context_window_source"] == "saved"
        assert listed["unknown"]["unusable_reason"] == "context window unknown"
    finally:
        db.close()


# --- The CORSIA checkpoint default (no saved window) ------------------------
# Nothing is stored for this: the default lives in the resolver only. A row
# with no saved window counts against min(discovered, DEFAULT_CHECKPOINT_WINDOW)
# on every harness, and a saved window still behaves exactly as it did.


def _row(harness, discovered, *, model_id="row-1", **extra):
    return {
        "id": model_id,
        "harness": harness,
        "context_window": discovered,
        **extra,
    }


@pytest.mark.parametrize(
    "harness", ["claude-code", "codex", "native", "external", "omlx"]
)
def test_an_unsaved_large_window_counts_against_the_default(harness):
    from llm import context_window as cw

    with patch.object(cw, "_profile_context_window", return_value=None):
        facts = cw.resolve_window_facts(_row(harness, 1_000_000))
    assert (facts.effective, facts.discovered, facts.source) == (
        cw.DEFAULT_CHECKPOINT_WINDOW, 1_000_000, "default",
    )


@pytest.mark.parametrize("harness", ["claude-code", "codex", "native", "external"])
def test_an_unsaved_window_below_the_default_counts_as_the_provider_reports_it(harness):
    from llm import context_window as cw

    with patch.object(cw, "_profile_context_window", return_value=None):
        facts = cw.resolve_window_facts(_row(harness, 200_000))
    assert (facts.effective, facts.discovered, facts.source) == (
        200_000, 200_000, "provider",
    )


def test_the_default_leaves_a_selector_that_names_the_bigger_window_alone():
    from llm import context_window as cw

    for model_id in ("claude-code/opus[1m]", "anthropic/claude-opus-5-5[1m]"):
        with patch.object(cw, "_profile_context_window", return_value=None):
            facts = cw.resolve_window_facts(
                _row("claude-code", 1_000_000, model_id=model_id)
            )
        assert (facts.effective, facts.discovered, facts.source) == (
            1_000_000, 1_000_000, "provider",
        ), model_id


def test_the_default_leaves_a_provider_model_id_naming_the_bigger_window_alone():
    from llm import context_window as cw

    row = _row(
        "claude-code", 1_000_000,
        model_id="claude-code/opus", provider_model_id="claude-opus-5-5[1m]",
    )
    with patch.object(cw, "_profile_context_window", return_value=None):
        facts = cw.resolve_window_facts(row)
    assert (facts.effective, facts.source) == (1_000_000, "provider")


def test_a_saved_window_still_lifts_a_harness_above_the_default():
    from llm import context_window as cw

    with patch.object(cw, "_profile_context_window", return_value=500_000):
        facts = cw.resolve_window_facts(_row("claude-code", 1_000_000))
    assert (facts.effective, facts.discovered, facts.source) == (
        500_000, 1_000_000, "saved",
    )


def test_clearing_a_saved_window_falls_back_to_the_default_not_the_provider():
    from llm import context_window as cw

    row = _row("claude-code", 1_000_000)
    with patch.object(cw, "_profile_context_window", return_value=500_000):
        saved = cw.resolve_window_facts(row)
    with patch.object(cw, "_profile_context_window", return_value=None):
        cleared = cw.resolve_window_facts(row)
    assert (saved.effective, saved.source) == (500_000, "saved")
    assert (cleared.effective, cleared.source) == (
        cw.DEFAULT_CHECKPOINT_WINDOW, "default",
    )


def test_a_native_saved_window_still_wins_over_the_default_and_the_provider():
    from llm import context_window as cw

    with patch.object(cw, "_profile_context_window", return_value=400_000):
        facts = cw.resolve_window_facts(_row("native", 200_000))
    assert (facts.effective, facts.discovered, facts.source) == (
        400_000, 200_000, "saved",
    )


def test_a_bracketed_name_that_is_not_a_window_does_not_escape_the_default():
    from llm import context_window as cw

    row = _row("native", 1_000_000, model_id="omlx@srv/Qwen3.8-Next[8bit]")
    with patch.object(cw, "_profile_context_window", return_value=None):
        facts = cw.resolve_window_facts(row)
    assert (facts.effective, facts.source) == (
        cw.DEFAULT_CHECKPOINT_WINDOW, "default",
    )


def test_no_window_is_written_when_the_default_is_in_force(isolated_db):
    from infra.database import get_db
    from llm import context_window as cw

    db = get_db()
    try:
        db.execute(
            """INSERT INTO models
               (id,harness,provider,provider_model_id,litellm_model,display_name,
                backend,source,enabled,available,context_window,created_at,updated_at)
               VALUES ('default-only','native','omlx','default-only',
                       'omlx@srv/default-only','Default Only','network',
                       'discovered',1,1,1000000,'t','t')"""
        )
        db.commit()
        row = db.execute("SELECT * FROM models WHERE id = 'default-only'").fetchone()
        facts = cw.resolve_window_facts(row, db=db)
        listed = cw.resolve_window_facts_for_rows([dict(row)], db=db)[0]
        assert (facts.effective, facts.source) == (
            cw.DEFAULT_CHECKPOINT_WINDOW, "default",
        )
        assert listed == facts
        assert db.execute(
            "SELECT COUNT(*) FROM model_profiles WHERE model_id = 'default-only'"
        ).fetchone()[0] == 0
    finally:
        db.close()
