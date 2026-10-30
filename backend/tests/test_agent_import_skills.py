"""Bringing over the person's own skills: offer states and installs (IMP-8..11, IMP-14)."""

from __future__ import annotations

import builtins
import datetime
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from onboarding import agent_import_skills as skills
from onboarding.project_discovery import Roots

NOW = datetime.datetime(2026, 10, 18, 12, tzinfo=datetime.timezone.utc)
#: Built at run time so no credential-shaped literal sits in the source.
SECRET_LINE = "api_" + "token=" + "fixture-" + "value-" + "9f8e7d6c5b4a"
ALL = ["claude-code", "codex", "openclaw", "hermes"]


def _skill(folder: Path, name: str, description: str, body: str = "Steps.\n", **extra) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    front = {"name": name, "description": description, **extra}
    (folder / "SKILL.md").write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body)
    return folder


@pytest.fixture
def home(tmp_path, monkeypatch):
    base = tmp_path / "home"
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    claude, codex = base / ".claude", base / ".codex"
    adr = _skill(claude / "skills" / "adr-writer", "adr-writer", "writes architecture decision records",
                 **{"allowed-tools": "Bash(*)", "hooks": {"pre": "x"}, "license": "MIT"})
    (adr / "scripts").mkdir()
    (adr / "scripts" / "new.py").write_text("print('adr')\n")
    os.chmod(adr / "scripts" / "new.py", 0o755)
    (adr / ".DS_Store").write_text("hidden")
    (claude / "commands" / "git").mkdir(parents=True)
    (claude / "commands" / "git" / "tidy.md").write_text("# Tidy the branch\nRebase and squash.\n")
    _skill(claude / "plugins" / "cache" / "p" / "skills" / "plugin-skill", "plugin-skill", "from a plugin")
    _skill(codex / "skills" / ".system" / "codex-own", "codex-own", "Codex's own")
    _skill(base / ".agents" / "skills" / "rfc-writer", "rfc-writer", "drafts RFCs for feature proposals")
    _skill(base / ".openclaw" / "workspace" / "skills" / "runbook-writer", "runbook-writer", "writes runbooks")
    _skill(base / ".hermes" / "skills" / "writing" / "style-guide", "style-guide", "applies the house style")
    _skill(claude / "skills" / "github", "github", "my own github helper")
    _skill(claude / "skills" / "deploy-helper", "deploy-helper", "deploys", body=SECRET_LINE + "\n")
    _skill(claude / "skills" / "deploy", "deploy", "deploys the Claude way")
    _skill(codex / "skills" / "deploy", "deploy", "deploys the Codex way")
    _skill(claude / "skills" / "shared-notes", "shared-notes", "same in both")
    _skill(codex / "skills" / "shared-notes", "shared-notes", "same in both")
    _skill(claude / "skills" / "agent-browser", "agent-browser", "already here")
    outside = _skill(tmp_path / "elsewhere" / "linked", "linked", "a linked folder")
    (claude / "skills" / "linked").symlink_to(outside)
    inner = _skill(claude / "skills" / "inner-link", "inner-link", "a link inside")
    (inner / "notes.md").symlink_to(outside / "SKILL.md")
    big = _skill(claude / "skills" / "big", "big", "too many files")
    for index in range(skills.MAX_FILES + 1):
        (big / f"f{index}.md").write_text("x")
    workspace = tmp_path / "workspace"
    _skill(workspace / "skills" / "agent-browser", "agent-browser", "the workspace copy")
    return Roots.for_user(), workspace


def _offers(home, tools=ALL):
    roots, workspace = home
    return {row["name"]: row for row in skills.skill_offers(roots, tools, workspace)}


def test_only_the_persons_own_skill_folders_are_offered(home):
    offers = _offers(home)
    assert {name for name, row in offers.items() if row["state"] == "new"} == {
        "adr-writer", "git-tidy", "rfc-writer", "runbook-writer", "style-guide", "shared-notes",
    }
    assert "plugin-skill" not in offers and "codex-own" not in offers


def test_each_state_is_decided_by_the_server(home):
    states = {name: row["state"] for name, row in _offers(home).items()}
    assert states["github"] == "name_reserved"
    assert states["agent-browser"] == "already_here"
    assert states["deploy"] == "name_clash"
    assert states["deploy-helper"] == "has_secret"
    assert states["big"] == "too_large"
    assert states["linked"] == "unreadable"
    assert states["inner-link"] == "unreadable"


def test_an_unreadable_skill_names_the_one_thing_to_fix(home, tmp_path):
    folder = tmp_path / "home" / ".claude" / "skills"
    (folder / "no-skill-md").mkdir()
    (folder / "no-skill-md" / "notes.md").write_text("notes")
    (_skill(folder / "binary", "binary", "has a binary file") / "logo.png").write_bytes(b"\x89PNG\x00")
    (folder / "bad-header").mkdir()
    (folder / "bad-header" / "SKILL.md").write_text("---\nname: [unclosed\n---\nBody.\n")
    bad_sidecar = _skill(folder / "bad-sidecar", "bad-sidecar", "has malformed corsia data")
    (bad_sidecar / "corsia.yaml").write_text("handler: [unclosed\n")
    _skill(folder / "bad-name", "bad name!", "a name with spaces")
    _skill(folder / "no-description", "no-description", "")
    offers = _offers(home)
    assert {name: offers[name]["reason"] for name in (
        "linked", "inner-link", "no-skill-md", "binary", "bad-header", "bad-name", "no-description",
    )} == {
        "linked": "link", "inner-link": "link", "no-skill-md": "no_skill_md", "binary": "not_text",
        "bad-header": "bad_header", "bad-name": "bad_name", "no-description": "no_description",
    }
    assert offers["bad-sidecar"]["state"] == "new"
    assert "reason" not in offers["adr-writer"] and "reason" not in offers["big"]


def test_a_key_is_reported_before_a_layout_problem(home, tmp_path):
    folder = tmp_path / "home" / ".claude" / "skills" / "keyed-no-description"
    _skill(folder, "keyed-no-description", "", body=SECRET_LINE + "\n")
    (folder.parent / "keyed-no-skill-md").mkdir()
    (folder.parent / "keyed-no-skill-md" / "notes.md").write_text(SECRET_LINE + "\n")
    offers = _offers(home)
    assert {offers[name]["state"] for name in ("keyed-no-description", "keyed-no-skill-md")} == {"has_secret"}


def test_the_agent_gets_metadata_never_a_body(home):
    offers = _offers(home)
    assert offers["adr-writer"] == {
        "item_id": offers["adr-writer"]["item_id"], "name": "adr-writer",
        "description": "writes architecture decision records", "tools": ["Claude Code"],
        "scripts": 1, "state": "new",
    }
    assert offers["adr-writer"]["item_id"].startswith("claude-code:~/.claude/skills/adr-writer:")
    assert offers["git-tidy"]["description"] == "Tidy the branch"
    assert offers["deploy-helper"]["description"] == ""
    assert offers["shared-notes"]["tools"] == ["Claude Code", "Codex"]
    assert "Steps." not in repr(offers) and "fixture-value" not in repr(offers)


def test_a_long_description_is_cut_at_a_word_within_500_characters():
    text = ("word " * 200).strip()
    shown = skills.shown_description(text + "\n\x07")
    assert len(shown) <= 500 and shown.endswith("word…")
    assert skills.shown_description("two\nlines") == "two lines"


def test_a_symlinked_or_oversize_skill_is_never_hashed_in_full(home, monkeypatch):
    opened: list[str] = []
    original = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), original(f, *a, **k))[1])
    _offers(home)
    assert not any("/big/" in path or "/elsewhere/" in path for path in opened)


def _ids(offers, *names):
    return [offers[name]["item_id"] for name in names]


def _install(home, item_ids, tools=ALL):
    roots, workspace = home
    return skills.install_skills(roots, tools, workspace, item_ids, now=NOW)


def test_install_writes_the_skill_data_form_and_nothing_tool_granting(home):
    roots, workspace = home
    results = _install(home, _ids(_offers(home), "adr-writer", "git-tidy"))
    assert [row["outcome"] for row in results] == ["installed", "installed"]
    assert set(results[0]) == {"item_id", "name", "sha256", "outcome", "history_outcome", "guidance_history"}
    target = workspace / "skills" / "adr-writer"
    meta = yaml.safe_load((target / "SKILL.md").read_text().split("---\n")[1])
    assert meta == {
        "name": "adr-writer", "description": "writes architecture decision records", "license": "MIT",
        "metadata": {"corsia-data": "corsia.yaml", "corsia-format": "1"},
    }
    sidecar = yaml.safe_load((target / "corsia.yaml").read_text())
    upstream = (roots.claude / "skills" / "adr-writer" / "SKILL.md").read_bytes()
    import hashlib

    assert sidecar["origin"] == {
        "source": "claude-code:~/.claude/skills/adr-writer",
        "sha256": hashlib.sha256(upstream).hexdigest(),
        "adopted_at": "2026-10-18T12:00:00+00:00",
        "installed_digest": sidecar["origin"]["installed_digest"],
    }
    from core.skill_sidecar import installed_digest

    assert sidecar["origin"]["installed_digest"] == installed_digest({
        path.relative_to(target).as_posix(): path.read_bytes() for path in target.rglob("*") if path.is_file()
    })
    assert stat.S_IMODE(os.stat(target / "scripts" / "new.py").st_mode) == 0o644
    assert not (target / ".DS_Store").exists()
    from core.skill_validation import validate_skill_file

    assert validate_skill_file(target / "SKILL.md").ok
    assert validate_skill_file(workspace / "skills" / "git-tidy" / "SKILL.md").ok
    history = subprocess.run(
        ["git", "-C", str(workspace), "log", "-1", "--format=%B"], capture_output=True, check=True, text=True
    ).stdout
    assert "Reason: consented skill import from agent_import" in history


def test_install_writes_the_description_that_was_shown(home):
    roots, workspace = home
    _skill(roots.codex / "skills" / "long-said", "long-said", "word " * 200)
    shown = _offers(home)["long-said"]["description"]

    assert _install(home, _ids(_offers(home), "long-said"))[0]["outcome"] == "installed"
    meta = yaml.safe_load((workspace / "skills" / "long-said" / "SKILL.md").read_text().split("---\n")[1])
    assert meta["description"] == shown
    assert len(shown) <= 500


def test_import_keeps_workflow_but_drops_upstream_handler(home):
    roots, workspace = home
    folder = _skill(roots.codex / "skills" / "with-handler", "with-handler", "carries a handler")
    (folder / "corsia.yaml").write_text("handler:\n  kind: cli\n  command: rm\nworkflow:\n  steps: []\n")

    assert _install(home, _ids(_offers(home), "with-handler"))[0]["outcome"] == "installed"
    sidecar = yaml.safe_load((workspace / "skills" / "with-handler" / "corsia.yaml").read_text())
    assert set(sidecar) == {"origin", "workflow"}
    assert sidecar["workflow"] == {"steps": []}
    assert sidecar["origin"]["sha256"] == hashlib.sha256((folder / "SKILL.md").read_bytes()).hexdigest()


def test_a_save_that_stops_partway_leaves_nothing_and_can_be_retried(home, monkeypatch):
    from infra import skill_install

    def partial(writes, **_kwargs):
        path, data = next(iter(writes.items()))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        raise OSError("disk full")

    offers = _offers(home)
    monkeypatch.setattr(skill_install, "save_guidance", partial)
    assert _install(home, _ids(offers, "rfc-writer"))[0]["outcome"] == "failed"
    assert not (home[1] / "skills" / "rfc-writer").exists()
    monkeypatch.undo()
    assert _offers(home)["rfc-writer"]["state"] == "new"


@pytest.mark.parametrize("outcome", ["deferred", "failed", "history-off"])
def test_a_save_without_history_is_installed_and_passes_its_line_on_unchanged(home, monkeypatch, outcome):
    from infra import skill_install
    from infra.guidance_git import SaveResult

    def unrecorded(writes, **_kwargs):
        for path, data in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return SaveResult(outcome, "history fault", line="Saved, but the commit failed. Report this.")

    monkeypatch.setattr(skill_install, "save_guidance", unrecorded)
    installed = _install(home, _ids(_offers(home), "rfc-writer"))[0]
    assert (installed["outcome"], installed["history_outcome"], installed["guidance_history"]) == (
        "installed", outcome, "Saved, but the commit failed. Report this.")
    assert (home[1] / "skills" / "rfc-writer" / "SKILL.md").exists()


@pytest.mark.parametrize(("outcome", "state"), [("stale", "already_here"), ("refused", "failed")])
def test_a_save_that_writes_nothing_keeps_its_line(home, monkeypatch, outcome, state):
    from infra import skill_install
    from infra.guidance_git import SaveResult

    monkeypatch.setattr(skill_install, "save_guidance", lambda _writes, **_kwargs: SaveResult(outcome, line="why"))
    result = _install(home, _ids(_offers(home), "rfc-writer"))[0]
    assert (result["outcome"], result["history_outcome"], result["guidance_history"]) == (state, outcome, "why")


def test_a_recorded_install_passes_the_save_line_on(home):
    result = _install(home, _ids(_offers(home), "rfc-writer"))[0]
    assert (result["outcome"], result["history_outcome"]) == ("installed", "written")
    assert result["guidance_history"].startswith("Saved and committed ")


def test_a_commit_with_an_unsynced_index_is_shown_and_never_removed(home, monkeypatch):
    from infra import skill_install
    from infra.guidance_git import SaveResult

    def landed(writes, **_kwargs):
        for path, data in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"other bytes")
        return SaveResult("written", "index-unsynced", sha="a" * 40, line="index sync failed",
                          unsynced=("skills/rfc-writer/SKILL.md",))

    monkeypatch.setattr(skill_install, "save_guidance", landed)
    result = _install(home, _ids(_offers(home), "rfc-writer"))[0]
    assert (result["outcome"], result["history_outcome"], result["guidance_history"]) == (
        "installed", "written", "index sync failed")
    assert (home[1] / "skills" / "rfc-writer" / "SKILL.md").exists()


def test_an_upstream_sidecar_in_another_case_is_dropped_and_a_case_clash_fails(home):
    roots, workspace = home
    folder = _skill(roots.codex / "skills" / "cased", "cased", "carries a cased sidecar")
    (folder / "Corsia.yaml").write_text("handler:\n  kind: cli\n  command: rm\n")
    assert _install(home, _ids(_offers(home), "cased"))[0]["outcome"] == "installed"
    sidecar = yaml.safe_load((workspace / "skills" / "cased" / "corsia.yaml").read_text())
    assert list(sidecar) == ["origin"]

    from infra.skill_install import install_skill_bundle

    clash = install_skill_bundle(
        workspace, "clash", (folder / "SKILL.md").read_bytes(), {"notes.md": b"a", "NOTES.md": b"b"},
        "clashes", {"source": "codex:~/.codex/skills/clash", "sha256": "0", "adopted_at": "now"},
        author_thread_id="t",
    )
    assert clash == {"outcome": "failed"}
    assert not (workspace / "skills" / "clash").exists()


def test_a_rerun_offers_nothing_twice(home):
    first = _offers(home)
    _install(home, _ids(first, "rfc-writer"))
    again = _offers(home)
    assert again["rfc-writer"]["state"] == "already_here"
    assert _install(home, _ids(first, "rfc-writer"))[0]["outcome"] in ("changed", "already_here")


def test_identical_content_imported_under_another_name_is_already_here(home):
    roots, workspace = home
    _install(home, _ids(_offers(home), "runbook-writer"))
    (workspace / "skills" / "runbook-writer").rename(workspace / "skills" / "runbooks")
    assert _offers(home)["runbook-writer"]["state"] == "already_here"


def test_only_new_items_install(home):
    offers = _offers(home)
    results = _install(home, _ids(offers, "github", "deploy", "deploy-helper", "big"))
    assert [row["outcome"] for row in results] == ["name_reserved", "name_clash", "has_secret", "too_large"]
    assert not (home[1] / "skills" / "github").exists()


def test_content_changed_after_the_offer_does_not_install(home):
    roots, _workspace = home
    offers = _offers(home)
    (roots.hermes / "skills" / "writing" / "style-guide" / "SKILL.md").write_text("changed")
    assert _install(home, _ids(offers, "style-guide"))[0]["outcome"] == "changed"


def test_install_never_overwrites_a_folder_already_there(home):
    offers = _offers(home)
    (home[1] / "skills" / "rfc-writer").mkdir(parents=True)
    assert _install(home, _ids(offers, "rfc-writer"))[0]["outcome"] == "already_here"
    assert list((home[1] / "skills" / "rfc-writer").iterdir()) == []


def test_a_held_back_skill_id_carries_no_digest_of_its_bytes(home):
    import hashlib

    roots, _workspace = home
    row = _offers(home)["deploy-helper"]
    assert row["state"] == "has_secret"
    assert row["item_id"] == "claude-code:~/.claude/skills/deploy-helper"
    raw = (roots.claude / "skills" / "deploy-helper" / "SKILL.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() not in repr(row)
    held = [r for r in _offers(home).values() if r["state"] != "new"]
    assert held and all(":" not in r["item_id"].split(":", 1)[1] for r in held)  # tool:~/path, no hash
    [true_sha] = [r.content_sha for r in skills.scan(roots, ALL, home[1]) if r.name == "deploy-helper"]
    right, wrong = (f"{row['item_id']}:{sha}" for sha in (true_sha, "0" * 64))
    assert [r["outcome"] for r in _install(home, [right, wrong])] == ["changed", "changed"]  # no guess confirmed


def test_a_key_outranks_a_reserved_or_clashing_name_and_confirms_no_guess(home):
    roots, workspace = home
    _skill(roots.codex / "skills" / "github", "github", "reserved name", body=SECRET_LINE + "\n")
    _skill(roots.hermes / "skills" / "ops" / "deploy", "deploy", "clashing name", body=SECRET_LINE + "\n")
    keyed = [row for row in skills.scan(roots, ALL, workspace)
             if row.source.startswith(("codex:~/.codex/skills/github", "hermes:"))
             and row.name in ("github", "deploy")]
    assert sorted(row.name for row in keyed) == ["deploy", "github"]
    assert {row.state for row in keyed} == {"has_secret"}
    guesses = [f"{row.source}:{row.content_sha}" for row in keyed]
    assert [r["outcome"] for r in _install(home, guesses)] == ["changed", "changed"]


def test_unknown_system_skill_names_hold_every_skill_back(home, monkeypatch):
    import core.system_tier

    offers = _offers(home)
    monkeypatch.setattr(core.system_tier, "system_skill_names", frozenset)
    assert {(row["state"], row["reason"]) for row in _offers(home).values()} == {("unreadable", "")}
    results = _install(home, _ids(offers, "adr-writer", "github"))
    assert all(row["outcome"] != "installed" for row in results)
    assert not (home[1] / "skills" / "adr-writer").exists() and not (home[1] / "skills" / "github").exists()


def test_a_linked_parent_folder_is_never_scanned(home, tmp_path):
    import shutil

    roots, _workspace = home
    for folder, moved in ((roots.openclaw / "workspace", "moved-claw"), (roots.claude / "commands", "moved-cmds")):
        shutil.move(folder, tmp_path / moved)
        folder.symlink_to(tmp_path / moved)
    offers = _offers(home)
    assert "runbook-writer" not in offers and "git-tidy" not in offers
    assert offers["adr-writer"]["state"] == "new"


def test_another_tools_metadata_blocks_are_dropped_on_install(home):
    roots, workspace = home
    _skill(roots.hermes / "skills" / "meeting-notes", "meeting-notes", "writes meeting notes",
           metadata={"author": "fixture", "openclaw": {"install": ["brew x"], "requires": {"bins": ["x"]}}})
    assert _install(home, _ids(_offers(home), "meeting-notes"))[0]["outcome"] == "installed"
    meta = yaml.safe_load((workspace / "skills" / "meeting-notes" / "SKILL.md").read_text().split("---\n")[1])
    assert meta["metadata"] == {"author": "fixture", "corsia-data": "corsia.yaml", "corsia-format": "1"}
    _skill(roots.hermes / "skills" / "standup", "standup", "runs standups",
           metadata={"tags": ["team"], "requires": [{"bins": ["x"]}]})
    assert _install(home, _ids(_offers(home), "standup"))[0]["outcome"] == "installed"
    meta = yaml.safe_load((workspace / "skills" / "standup" / "SKILL.md").read_text().split("---\n")[1])
    assert meta["metadata"] == {"tags": ["team"], "corsia-data": "corsia.yaml", "corsia-format": "1"}


def test_synced_bucket_without_manifest_is_not_offered(tmp_path, monkeypatch):
    """A bucket without a readable manifest cannot identify personal skills."""
    base = tmp_path / "home"
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    synced = base / ".claude" / "skills" / "synced"
    bundle = synced / "account_bundle" / "xlsx"
    bundle.mkdir(parents=True)
    for index in range(skills.MAX_FILES + 1):
        (bundle / f"f{index}.md").write_text("x")
    (synced / ".bucket-account_bundle").write_text("")
    workspace = tmp_path / "workspace"
    assert skills.skill_offers(Roots.for_user(), ["claude-code"], workspace) == []
    _skill(base / ".claude" / "skills" / "mine", "mine", "a skill the person made")
    assert [row["name"] for row in skills.skill_offers(Roots.for_user(), ["claude-code"], workspace)] == ["mine"]


def _synced_bucket(root, bucket_name, entries):
    synced = root / "skills" / "synced"
    bucket = synced / bucket_name
    bucket.mkdir(parents=True)
    (synced / f".bucket-{bucket_name}").write_text("")
    (bucket / "manifest.json").write_text(json.dumps({"lastUpdated": "2026-10-20T12:00:00Z", "skills": entries}))
    for entry in entries:
        _skill(bucket / entry["name"], entry["name"], entry.get("description", "a synced skill"))
    return bucket


def test_synced_manifest_excludes_builtins_and_offers_personal_skills(home):
    roots, workspace = home
    entries = [
        {"name": "pdf", "source": "anthropic", "updatedAt": "2026-10-20T12:00:00Z"},
        {"name": "morning", "source": "anthropic-example", "updatedAt": "2026-10-20T12:00:00Z"},
        {"name": "my-recipe", "source": "custom", "updatedAt": "2026-10-20T12:00:00Z"},
    ]
    bucket = _synced_bucket(roots.claude, "org_account", entries)
    for index in range(skills.MAX_FILES + 1):
        (bucket / "pdf" / f"f{index}.md").write_text("x")
    offers = skills.skill_offers(roots, ["claude-code"], workspace)
    names = {row["name"] for row in offers}
    assert "my-recipe" in names
    assert "pdf" not in names and "morning" not in names
    assert next(row for row in offers if row["name"] == "my-recipe")["state"] == "new"


def test_synced_builtins_only_produce_no_offers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    roots = Roots.for_user()
    bucket = _synced_bucket(roots.claude, "org_account", [
        {"name": "xlsx", "source": "anthropic", "updatedAt": "2026-10-20T12:00:00Z"},
    ])
    for index in range(skills.MAX_FILES + 1):
        (bucket / "xlsx" / f"f{index}.md").write_text("x")
    assert skills.skill_offers(roots, ["claude-code"], tmp_path / "workspace") == []


def test_synced_newest_account_wins_and_unreadable_manifest_is_skipped(home):
    roots, workspace = home
    _synced_bucket(roots.claude, "old_account", [
        {"name": "my-recipe", "source": "custom", "updatedAt": "2026-10-19T12:00:00Z", "description": "old"},
    ])
    _synced_bucket(roots.claude, "new_account", [
        {"name": "my-recipe", "source": "custom", "updatedAt": "2026-10-20T12:00:00Z", "description": "new"},
    ])
    broken = _synced_bucket(roots.claude, "broken_account", [
        {"name": "hidden", "source": "custom", "updatedAt": "2026-10-20T12:00:00Z"},
    ])
    (broken / "manifest.json").write_text("invalid")
    offers = skills.skill_offers(roots, ["claude-code"], workspace)
    matching = [row for row in offers if row["name"] == "my-recipe"]
    assert len(matching) == 1 and matching[0]["description"] == "new"
    assert "hidden" not in {row["name"] for row in offers}


def test_synced_default_source_installs_and_invalid_paths_are_not_offered(home, tmp_path):
    roots, workspace = home
    bucket = _synced_bucket(roots.claude, "org_account", [{"name": "my-recipe"}])
    outside = _skill(tmp_path / "outside", "outside", "linked outside")
    (bucket / "linked").symlink_to(outside)
    (bucket / "manifest.json").write_text(json.dumps({"skills": [
        {"name": "my-recipe"}, {"name": "linked", "source": "custom"},
        {"name": "..", "source": "custom"}, {"name": "a/b", "source": "custom"},
        {"name": "../outside", "source": "custom"}, {"name": ".hidden", "source": "custom"},
    ]}))
    offers = skills.skill_offers(roots, ["claude-code"], workspace)
    rows = {row["name"]: row for row in offers}
    assert rows["my-recipe"]["state"] == "new"
    assert rows["linked"]["state"] == "unreadable"
    assert not {"..", "a/b", "../outside", ".hidden"} & rows.keys()
    assert "/synced/org_account/my-recipe:" in rows["my-recipe"]["item_id"]
    result = skills.install_skills(roots, ["claude-code"], workspace, [rows["my-recipe"]["item_id"]])
    assert result[0]["outcome"] == "installed"
    assert (workspace / "skills" / "my-recipe" / "SKILL.md").is_file()


def test_synced_equal_and_missing_timestamps_use_first_bucket(home):
    roots, workspace = home
    for bucket, description in (("z_account", "second"), ("a_account", "first")):
        _synced_bucket(roots.claude, bucket, [
            {"name": "equal-time", "updatedAt": "2026-10-20T12:00:00Z", "description": description},
            {"name": "missing-time", "description": description},
        ])
    offers = {row["name"]: row for row in skills.skill_offers(roots, ["claude-code"], workspace)}
    assert offers["equal-time"]["description"] == "first"
    assert offers["missing-time"]["description"] == "first"
