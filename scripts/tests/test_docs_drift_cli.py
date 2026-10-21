"""Tests for CLI argument parsing and exit-code wiring (check_docs_drift.cli)."""

from __future__ import annotations

import json
import subprocess

import pytest

from check_docs_drift import cli, owner_identity, private_scan_check as scan


def _minimal_repo(tmp_path, monkeypatch):
    (tmp_path / "backend" / "api").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "corsia").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "nonexistent-gitconfig"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Alice Anderson"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    return tmp_path


def test_build_parser_defaults_require_owner_identity():
    args = cli.build_parser().parse_args([])

    assert args.allow_missing_owner_identity is False
    assert args.owner_name is None


def test_main_passes_with_an_owner_name(tmp_path, monkeypatch, capsys):
    _minimal_repo(tmp_path, monkeypatch)

    exit_code = cli.main(["--owner-name", "Alice Anderson"])

    assert exit_code == 0
    assert "PASS" in capsys.readouterr().out


def test_json_out_preserves_text_stdout(tmp_path, monkeypatch, capsys):
    import json

    _minimal_repo(tmp_path, monkeypatch)
    output = tmp_path / "result.json"
    exit_code = cli.main(["--owner-name", "Alice", "--json-out", str(output)])

    assert exit_code == 0
    assert capsys.readouterr().out.startswith("# Documentation drift report")
    assert json.loads(output.read_text(encoding="utf-8"))["private_names"]["owner_identity_skipped_reason"] is None


def test_main_json_output_is_valid_json(tmp_path, monkeypatch, capsys):
    import json

    _minimal_repo(tmp_path, monkeypatch)

    cli.main(["--owner-name", "Alice Anderson", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def _owner_identity_case(tmp_path, monkeypatch, owner_lines):
    repo = _minimal_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("Maintained by Bea Brown.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    owner_file = repo / "fake-home" / ".owner-identity"
    owner_file.write_text(owner_lines, encoding="utf-8")
    return ["--owner-identity-file", str(owner_file)]


def test_owner_identity_file_overrides_git_identity(tmp_path, monkeypatch, capsys):
    args = _owner_identity_case(tmp_path, monkeypatch, "Bea Brown\nbea@example.test\n")

    exit_code = cli.main(args)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "OWNER IDENTITY: README.md: ['Bea Brown']" in out
    assert "Alice Anderson" not in out


def test_empty_owner_identity_file_fails_when_identity_is_required(tmp_path, monkeypatch, capsys):
    args = _owner_identity_case(tmp_path, monkeypatch, "\n")

    exit_code = cli.main(args)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "owner-identity sub-check SKIPPED" in out
    assert "no --owner-name, --owner-identity-file, or usable --owner-profile" in out


def test_missing_owner_identity_file_is_a_usage_error(tmp_path, monkeypatch):
    _minimal_repo(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["--owner-identity-file", str(tmp_path / "absent")])

    assert exc.value.code == 2


def test_owner_name_takes_precedence_over_owner_identity_file(tmp_path, monkeypatch, capsys):
    args = _owner_identity_case(tmp_path, monkeypatch, "Bea Brown\nbea@example.test\n")

    exit_code = cli.main([*args, "--owner-name", "Alice Anderson"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OWNER IDENTITY" not in out


def _profile(path):
    path.write_text(
        "## Basics\n- **Name:** Alice Example\n- **Goes by:** Ally\n"
        "## Contact\n- **Phone:** +1 555-010-2345\n- **Work:** alice@example.test\n",
        encoding="utf-8",
    )


def _profile_hit(tmp_path, monkeypatch, planted):
    repo = _minimal_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text(planted, encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    profile = tmp_path / "USER.md"
    _profile(profile)
    return cli.main(["--owner-profile", str(profile)])


def test_profile_name_phrase_is_case_insensitive_and_literal(tmp_path, monkeypatch, capsys):
    assert _profile_hit(tmp_path, monkeypatch, "alice example") == 1
    assert "OWNER IDENTITY: README.md: ['Alice Example']" in capsys.readouterr().out


def test_profile_name_phrase_has_word_boundaries(tmp_path, monkeypatch):
    repo = _minimal_repo(tmp_path, monkeypatch)
    profile = tmp_path / "USER.md"
    profile.write_text("# User Info\nName: Rae\n", encoding="utf-8")

    assert scan._person_name_hits("raelyn", ["Rae"], in_fixture_path=False) == []
    assert scan._person_name_hits("standalone Rae", ["Rae"], in_fixture_path=False) == ["Rae"]

    (repo / "README.md").write_text("standalone Rae\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    assert cli.main(["--owner-profile", str(profile)]) == 1


def test_lowercase_id_seed_exemption_is_scoped_to_a_fixture_seed_value():
    """The carve-out is the seed itself, inside a test/fixture path -- not any
    line that happens to contain ``*_id = "``."""
    assert scan._person_name_hits('user_id = "rae"', ["Rae"], in_fixture_path=True) == []
    assert scan._person_name_hits('user_id = "rae"', ["Rae"], in_fixture_path=False) == ["Rae"]
    assert scan._person_name_hits('user_id = "Rae"', ["Rae"], in_fixture_path=True) == ["Rae"]

    line = 'owner_id = "x", author = "Alice Example"'
    assert scan._person_name_hits(line, ["Alice Example"], in_fixture_path=True) == ["Alice Example"]
    assert scan._person_name_hits(line, ["Alice Example"], in_fixture_path=False) == ["Alice Example"]

    assert scan._is_fixture_path("backend/tests/test_x.py") is True
    assert scan._is_fixture_path("backend/eval/fixtures/stub_workspace.py") is True
    assert scan._is_fixture_path("README.md") is False


def test_each_token_kind_matches_by_its_stated_rule():
    """Person and contact tokens ignore case; registry literals keep theirs."""
    assert scan._literal_hits("alice@example.test", ["Alice@Example.test"], case_insensitive=True) == [
        "Alice@Example.test"
    ]
    assert scan._literal_hits("alice@example.test", ["Alice@Example.test"]) == []
    assert scan._person_name_hits("by ALICE EXAMPLE today", ["Alice Example"], in_fixture_path=False) == [
        "Alice Example"
    ]


def test_registry_tokens_keep_the_case_the_maintainer_chose(tmp_path, monkeypatch):
    repo = _minimal_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("Maintained by bea brown.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    owner_file = repo / "fake-home" / ".owner-identity"
    owner_file.write_text("Bea Brown\n", encoding="utf-8")

    assert cli.main(["--owner-identity-file", str(owner_file)]) == 0


def test_profile_contact_tokens_match_any_case(tmp_path, monkeypatch, capsys):
    assert _profile_hit(tmp_path, monkeypatch, "ALICE@EXAMPLE.TEST") == 1
    assert "OWNER IDENTITY: README.md: ['alice@example.test']" in capsys.readouterr().out


def test_the_first_labeled_name_is_the_owner(tmp_path):
    """A later section may list other people; it must not replace the owner."""
    profile = tmp_path / "USER.md"
    profile.write_text(
        "# Basics\nName: Alice Example\n\n## Family\n- **Name:** Bob Other\n", encoding="utf-8"
    )

    tokens = owner_identity.read_owner_profile(profile)

    assert tokens.person_names == ["Alice Example"]
    assert "person" in tokens.kinds


def test_another_person_named_in_the_profile_is_not_scanned_as_the_owner(
    tmp_path, monkeypatch, capsys
):
    repo = _minimal_repo(tmp_path, monkeypatch)
    profile = tmp_path / "USER.md"
    profile.write_text(
        "# Basics\nName: Alice Example\n\n## Family\n- **Name:** Bob Other\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("Mentions Bob Other only.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)

    assert cli.main(["--owner-profile", str(profile)]) == 0
    assert "OWNER IDENTITY" not in capsys.readouterr().out


def test_a_name_value_is_validated_never_repaired(tmp_path):
    """Accepted shapes yield the phrase; anything else yields no name token.

    The gate may only claim the name check for a value that could match prose.
    A run-on after a comma, a parenthetical, a list, markup, or an email is not
    a person-name phrase, so the person kind does not resolve and says why --
    instead of inventing a token that can never match and calling it coverage.
    """
    profile = tmp_path / "USER.md"

    def resolve(raw):
        profile.write_text(f"# Basics\n{raw}\n", encoding="utf-8")
        return owner_identity.read_owner_profile(profile)

    assert resolve("Name: Eval User.").person_names == ["Eval User"]
    assert resolve("- **Name:** Alice Example").person_names == ["Alice Example"]
    assert resolve("Name: Jean-Luc O'Brien").person_names == ["Jean-Luc O'Brien"]

    for rejected in (
        "Name: Alice Example, who owns the plans and the contact details",
        "Name: Alice Example (she/her)",
        "Name: Alice Example, Bob Other",
        "Name: `Alice Example`",
        "Name: alice@example.test",
    ):
        tokens = resolve(rejected)
        assert tokens.person_names == [], rejected
        assert "person" not in tokens.kinds
        assert "person" in tokens.kind_reasons


def test_person_token_boundary_rejects_a_longer_word_and_allows_an_underscore():
    """`rae` inside `raelyn` is not a name; a name inside an identifier is.

    The boundary is "not preceded or followed by an alphanumeric", and '_' is
    deliberately allowed on both sides: `ask_rae`, `rae_id` and an
    underscore-joined full name are precisely where a name leaks.
    """
    assert scan._person_name_hits("raelyn", ["Rae"], in_fixture_path=False) == []
    assert scan._person_name_hits("standalone Rae", ["Rae"], in_fixture_path=False) == ["Rae"]
    assert scan._person_name_hits("ask_rae_now", ["Rae"], in_fixture_path=False) == ["Rae"]
    assert (
        scan._person_name_hits("by ALICE EXAMPLE today", ["Alice Example"], in_fixture_path=False)
        == ["Alice Example"]
    )
    assert (
        scan._person_name_hits("the alice_example record", ["Alice Example"], in_fixture_path=False)
        == ["Alice Example"]
    )


def test_one_number_matches_whatever_separators_or_country_code_a_file_uses(tmp_path, monkeypatch):
    """The four ways to write the same number are one token, not four strings."""
    repo = _minimal_repo(tmp_path, monkeypatch)
    profile = tmp_path / "USER.md"
    profile.write_text(
        "# Basics\nName: Rae\n\n## Contact\n- **Phone:** +1 555-010-2345\n", encoding="utf-8"
    )

    tokens = owner_identity.read_owner_profile(profile)
    assert [token.literal for token in tokens.phone_tokens] == ["+1 555-010-2345"]
    assert tokens.phone_tokens[0].reported_forms == ["+1 555-010-2345", "15550102345"]
    patterns = scan._phone_patterns(tokens.phone_tokens)

    for written in ("+1 555-010-2345", "555-010-2345", "(555) 010-2345", "5550102345"):
        assert scan._phone_hits(written, patterns), written

    (repo / "README.md").write_text("Call 555-010-2345.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    assert cli.main(["--owner-profile", str(profile)]) == 1


def test_dates_and_clause_boundaries_never_become_a_phone_token(tmp_path):
    """A ' - ' sequence separates clauses, and a run needs >=9 digits.

    Without both rules, `2026-10-01 - 2026-10-05` merges into one 16-digit token
    and every ISO date in the repository reads as an owner hit.
    """
    profile = tmp_path / "USER.md"
    profile.write_text(
        "# Basics\nName: Rae\n\n### Contact\n- **Updated:** 2026-10-03\n"
        "- Window: 2026-10-01 - 2026-10-05\n",
        encoding="utf-8",
    )

    tokens = owner_identity.read_owner_profile(profile)

    assert tokens.phone_tokens == []
    assert tokens.kinds == ["person"]
    assert tokens.kind_reasons["contact"].startswith("profile has no email or phone literal")


def test_the_run_reports_which_owner_kinds_actually_ran(tmp_path, monkeypatch):
    """A contact-only run must never read as a completed name check."""
    _minimal_repo(tmp_path, monkeypatch)
    profile = tmp_path / "USER.md"
    profile.write_text(
        "# Basics\nGoes by: Ally\n\n## Contact\n- **Work:** alice@example.test\n", encoding="utf-8"
    )
    json_out = tmp_path / "report.json"

    assert cli.main(["--owner-profile", str(profile), "--json-out", str(json_out)]) == 0
    private = json.loads(json_out.read_text(encoding="utf-8"))["private_names"]

    assert private["owner_identity_kinds"] == ["contact"]
    assert private["owner_identity_skipped_reason"] is None
    assert "person" in private["owner_identity_kind_reasons"]


def test_profile_accepts_labeled_name_anywhere_and_contact_heading_variants(tmp_path):
    profile = tmp_path / "USER.md"
    _profile(profile)

    tokens = owner_identity.read_owner_profile(profile)

    assert tokens.person_names == ["Alice Example"]
    assert tokens.email_tokens == ["alice@example.test"]
    assert [token.literal for token in tokens.phone_tokens] == ["+1 555-010-2345"]
    assert tokens.kinds == ["person", "contact"]

    profile.write_text(
        "# User (eval)\n\nName: Eval User.\n\n## Contact details\n"
        "- **Updated:** 2026-10-03\n- **Phone:** +1 555-010-2345\n",
        encoding="utf-8",
    )
    tokens = owner_identity.read_owner_profile(profile)

    assert tokens.person_names == ["Eval User"]
    assert tokens.email_tokens == []
    assert [token.reported_forms for token in tokens.phone_tokens] == [["+1 555-010-2345", "15550102345"]]


def test_the_eval_stub_profile_shape_still_matches_prose(tmp_path, monkeypatch, capsys):
    """The shape backend/eval/fixtures/stub_workspace.py ships must remain usable."""
    repo = _minimal_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("Owned by Eval User today.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    profile = tmp_path / "USER.md"
    profile.write_text("# User (eval)\n\nName: Eval User.\n", encoding="utf-8")

    assert cli.main(["--owner-profile", str(profile)]) == 1
    assert "OWNER IDENTITY: README.md: ['Eval User']" in capsys.readouterr().out


def test_a_profile_with_nothing_supported_names_the_profile_path(tmp_path):
    profile = tmp_path / "USER.md"
    profile.write_text("## Basics\nGoes by: Ally\n", encoding="utf-8")

    tokens = owner_identity.read_owner_profile(profile)

    assert tokens.kinds == []
    assert tokens.skip_reason == (
        f"profile has no labeled Name field: {profile}; "
        f"profile has no email or phone literal under a heading containing 'contact': {profile}"
    )


def test_missing_owner_profile_is_a_usage_error(tmp_path, monkeypatch):
    _minimal_repo(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["--owner-profile", str(tmp_path / "absent")])

    assert exc.value.code == 2


def test_owner_file_overrides_profile(tmp_path, monkeypatch, capsys):
    args = _owner_identity_case(tmp_path, monkeypatch, "Bea Brown\n")
    profile = tmp_path / "USER.md"
    _profile(profile)

    exit_code = cli.main([*args, "--owner-profile", str(profile)])

    assert exit_code == 1
    assert "Bea Brown" in capsys.readouterr().out
