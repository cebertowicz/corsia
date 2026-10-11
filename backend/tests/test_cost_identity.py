"""One model identity per usage row, read from the manifest and catalog owners."""

from __future__ import annotations

import pytest

from api.cost_identity import IdentityResolver
from model_identity_fixtures import LOCAL_ID, LOCAL_MODEL, seed_identity_catalog


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    seed_identity_catalog(connection)
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _row(model: str, harness: str | None = None, reported: str | None = None, source: str | None = None) -> dict:
    return {"model": model, "manifest_harness": harness, "manifest_model": reported, "manifest_source": source}


def test_harness_manifest_identity_outranks_the_alias_it_ran_under(db):
    identity = IdentityResolver(db)(_row("claude-code/opus", "claude-code", "claude-opus-5-5[1m]", "catalog"))
    assert (identity.key, identity.resolved) == ("claude-code/claude-opus-5-5", True)


def test_native_manifest_wire_alias_does_not_replace_the_catalog_model(db):
    identity = IdentityResolver(db)(_row(LOCAL_ID, "native", "Fortissimo-8bit", "transport"))
    assert identity.key == LOCAL_MODEL


def test_local_catalog_id_resolves_to_its_model_and_name(db):
    identity = IdentityResolver(db)(_row(LOCAL_ID))
    assert (identity.key, identity.label, identity.resolved) == (LOCAL_MODEL, "Fortissimo", True)


@pytest.mark.parametrize("literal", ["claude-code/opus", "claude-code/opus[1m]"])
def test_alias_nothing_resolves_stays_literal_and_unresolved(db, literal):
    """No date or current-mapping guess: the seeded alias names claude-opus-4-8 today."""
    identity = IdentityResolver(db)(_row(literal))
    assert (identity.key, identity.resolved) == ("claude-code/opus", False)


@pytest.mark.parametrize("literal", ["claude-code/opus", "claude-code/opus[1m]"])
def test_unresolved_alias_is_named_by_its_selector_not_a_versioned_catalog_name(db, literal):
    """The catalog calls the alias Opus 4.8, the model it names today; that version is not recorded."""
    assert IdentityResolver(db)(_row(literal)).label == "opus (version not recorded)"


def test_spark_is_its_own_identity(db):
    identity = IdentityResolver(db)(_row("codex/gpt-5.3-codex-spark"))
    assert (identity.key, identity.resolved) == ("codex/gpt-5.3-codex-spark", True)


def test_same_suffix_from_two_providers_stays_two_identities(db):
    resolve = IdentityResolver(db)
    assert resolve(_row("codex/gpt-5-codex")).key != resolve(_row("openai/gpt-5-codex")).key


def test_labels_come_from_the_catalog_never_a_suffix(db):
    resolve = IdentityResolver(db)
    assert resolve(_row("claude-code/claude-opus-4-8")).label == "Opus 4.8"
    assert resolve(_row("openai/gpt-5-codex")).label == "openai/gpt-5-codex"


@pytest.mark.parametrize(("literal", "key", "label"), [
    ("claude-code/claude-sonnet-5-5", "claude-code/claude-sonnet-5-5", "claude-sonnet-5-5"),
    ("claude-code/claude-sonnet-5-5[1m]", "claude-code/claude-sonnet-5-5", "claude-sonnet-5-5"),
    ("codex/gpt-5.2-codex", "codex/gpt-5.2-codex", "gpt-5.2-codex"),
])
def test_a_model_no_catalog_row_names_is_shown_without_its_harness_prefix(db, literal, key, label):
    """A model no longer offered is the one most likely to have lost its catalog row; it is still a model, not a harness."""
    identity = IdentityResolver(db)(_row(literal))
    assert (identity.key, identity.label, identity.resolved) == (key, label, True)


def test_a_catalog_model_no_longer_offered_keeps_its_display_name(db):
    resolve = IdentityResolver(db)
    identity = resolve(_row("claude-code/claude-opus-4-8"))
    offered = resolve.offered_now([{"litellm_model": "claude-code/sonnet"}])
    assert (identity.label, identity.key in offered) == ("Opus 4.8", False)


def test_offered_now_names_an_alias_by_what_it_resolves_to_today(db):
    offered = IdentityResolver(db).offered_now([{"litellm_model": "claude-code/sonnet"}])
    assert offered == {"claude-code/sonnet", "claude-code/claude-sonnet-4-8"}


@pytest.mark.parametrize(("key", "label"), [
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
    ("external@ns-96c32a544b/qwen3.8-flash", "Qwen 3.8 Flash"),
    ("glm-5.3", "GLM-5.3"),
    ("some-other-model", "some-other-model"),
])
def test_a_bare_model_id_the_page_names_reads_as_its_name_in_every_label(db, key, label):
    assert IdentityResolver(db)(_row(key)).label == label
