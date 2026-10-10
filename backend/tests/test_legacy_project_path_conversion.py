from __future__ import annotations

from pathlib import Path

import pytest

from infra.platform_paths import WorkspaceSelection


def _selection(root: Path) -> WorkspaceSelection:
    return WorkspaceSelection(
        workspace_id="workspace-test",
        root=root.resolve(),
        name="Test workspace",
    )


def _insert_projects(db, rows: list[tuple[str, str, str | None]]) -> None:
    db.executemany(
        "INSERT INTO projects (id, name, path, created_at, updated_at) "
        "VALUES (?, ?, ?, 't', 't')",
        rows,
    )
    db.commit()


def _stored_paths(db) -> dict[str, str | None]:
    return {
        str(row["id"]): row["path"]
        for row in db.execute("SELECT id, path FROM projects ORDER BY id")
    }


def test_startup_converter_preserves_old_resolver_choices_and_is_idempotent(
    isolated_db,
    tmp_path,
    monkeypatch,
):
    import api.project_scaffolding as scaffolding
    import core.project_path_conversion as conversion
    from core.project_context import load_project_context, resolve_project_paths
    from core.skill_resolution import project_dir
    from infra.database import get_db
    from infra.project_repo_policy import _source_binding_error, get_project_repo_policies

    monkeypatch.setattr(scaffolding.Path, "home", classmethod(lambda cls: tmp_path))

    logged: list[str] = []

    class CapturingLogger:
        def warning(self, message: str, *args: object) -> None:
            logged.append(message.format(*args))

        def info(self, message: str, *args: object) -> None:
            logged.append(message.format(*args))

    monkeypatch.setattr(conversion, "logger", CapturingLogger())

    workspace = tmp_path / "workspace"
    projects = workspace / "projects"
    external = tmp_path / "external"
    for child in (
        "alpha",
        "empty-space",
        "under",
        "copied",
        "by-slug",
        "basename-only",
    ):
        (projects / child).mkdir(parents=True)
    (external / "projects" / "copied").mkdir(parents=True)
    (external / "code" / "elsewhere").mkdir(parents=True)
    (external / "code" / "basename-only").mkdir(parents=True)

    import infra.config as config

    monkeypatch.setattr(config, "WORKSPACE_SELECTION", _selection(workspace))
    logical = "workspace://workspace-test/projects/already-logical"
    rows = [
        ("p-null", "Alpha", None),
        ("p-empty", "Empty Space", ""),
        ("p-under", "Different Name", str(projects / "under")),
        (
            "p-foreign-shape",
            "Different Slug",
            str(external / "projects" / "copied"),
        ),
        ("p-foreign-slug", "By Slug", str(external / "code" / "elsewhere")),
        (
            "p-basename-ignored",
            "No Match",
            str(external / "code" / "basename-only"),
        ),
        ("p-missing", "Missing", str(external / "projects" / "missing")),
        ("p-logical", "Already Logical", logical),
    ]
    db = get_db()
    try:
        _insert_projects(db, rows)

        first = conversion.convert_legacy_project_paths(_selection(workspace), db=db)
        first_paths = _stored_paths(db)
        policies = get_project_repo_policies("p-foreign-slug", db=db)
        foreign_shape_policies = get_project_repo_policies("p-foreign-shape", db=db)
        created_policies = get_project_repo_policies("p-basename-ignored", db=db)
        missing_paths = resolve_project_paths(workspace, "p-missing", db=db)
        missing_context = load_project_context(workspace, "p-missing", db=db)
        missing_dir = project_dir("p-missing", workspace, db)
        first_log_count = len(logged)
        second = conversion.convert_legacy_project_paths(_selection(workspace), db=db)
        second_paths = _stored_paths(db)
    finally:
        db.close()

    prefix = "workspace://workspace-test/projects/"
    assert first.converted == 7
    assert first.kept_legacy == 0
    assert first_paths == {
        "p-basename-ignored": f"{prefix}no-match",
        "p-empty": f"{prefix}empty-space",
        "p-foreign-shape": f"{prefix}copied",
        "p-foreign-slug": f"{prefix}by-slug",
        "p-logical": logical,
        "p-missing": f"{prefix}missing",
        "p-null": f"{prefix}alpha",
        "p-under": f"{prefix}under",
    }
    assert (second.converted, second.kept_legacy) == (0, 0)
    assert second_paths == first_paths
    assert logged[first_log_count:] == []
    assert list(policies) == ["elsewhere"]
    assert policies["elsewhere"].path == external / "code" / "elsewhere"
    assert _source_binding_error(projects / "by-slug", policies["elsewhere"]) is None
    # The selected folder supersedes a stored project-folder path: never a repo.
    assert not foreign_shape_policies
    assert not (projects / "copied" / "source").exists()
    # A project with no folder gets the standard one, with its checkout linked.
    assert (projects / "no-match" / "context").is_dir()
    assert created_policies["basename-only"].path == external / "code" / "basename-only"
    assert missing_paths == ("Missing", (projects / "missing").resolve())
    assert missing_context is not None
    assert missing_dir == (projects / "missing").resolve()
    assert logged[first_log_count - 1] == (
        "legacy project path conversion: converted=7 kept_legacy=0"
    )


def test_unreachable_checkout_is_kept_then_converts_once_it_returns(
    isolated_db, tmp_path, monkeypatch
):
    import api.project_scaffolding as scaffolding
    import core.project_path_conversion as conversion
    from infra.database import get_db
    from infra.project_repo_policy import get_project_repo_policies

    monkeypatch.setattr(scaffolding.Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    (workspace / "projects" / "alpha").mkdir(parents=True)
    checkout = tmp_path / "volume" / "code" / "repo"
    db = get_db()
    try:
        _insert_projects(db, [("p", "Alpha", str(checkout))])
        unmounted = conversion.convert_legacy_project_paths(_selection(workspace), db=db)
        kept_path = _stored_paths(db)["p"]
        kept_policies = get_project_repo_policies("p", db=db)
        checkout.mkdir(parents=True)
        mounted = conversion.convert_legacy_project_paths(_selection(workspace), db=db)
        converted_path = _stored_paths(db)["p"]
        policies = get_project_repo_policies("p", db=db)
    finally:
        db.close()

    assert (unmounted.converted, unmounted.kept_legacy) == (0, 1)
    assert kept_path == str(checkout)
    assert not kept_policies
    assert (mounted.converted, mounted.kept_legacy) == (1, 0)
    assert converted_path == "workspace://workspace-test/projects/alpha"
    assert policies["repo"].path == checkout.resolve()


def test_kept_row_without_folder_gets_one_and_can_send(isolated_db, tmp_path, monkeypatch):
    import api.project_scaffolding as scaffolding
    import core.project_path_conversion as conversion
    import infra.config as config
    from core.project_context import resolve_project_paths
    from infra.database import get_db
    from infra.send_project_paths import validate_project_working_dir

    monkeypatch.setattr(scaffolding.Path, "home", classmethod(lambda cls: tmp_path))
    workspace = config.WORKSPACE
    checkout = tmp_path / "volume" / "code" / "repo"
    db = get_db()
    try:
        _insert_projects(db, [("p", "Orphan", str(checkout))])
        first = conversion.convert_legacy_project_paths(config.WORKSPACE_SELECTION, db=db)
        second = conversion.convert_legacy_project_paths(config.WORKSPACE_SELECTION, db=db)
        stored = _stored_paths(db)["p"]
        resolved = resolve_project_paths(workspace, "p", db=db)
    finally:
        db.close()

    folder = (workspace / "projects" / "orphan").resolve()
    assert (first.converted, first.kept_legacy) == (0, 1)
    assert (second.converted, second.kept_legacy) == (0, 1)
    assert stored == str(checkout)
    assert resolved == ("Orphan", folder)
    assert (folder / "context").is_dir()
    validate_project_working_dir("p", harness="Codex")


def test_converter_never_overwrites_taken_policy_or_source_name(isolated_db, tmp_path, monkeypatch):
    import api.project_scaffolding as scaffolding
    import core.project_path_conversion as conversion
    from infra.database import get_db
    from infra.project_repo_policy import get_project_repo_policies, register_repo_policy

    monkeypatch.setattr(scaffolding.Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    planning = workspace / "projects" / "alpha"
    checkout = tmp_path / "legacy" / "elsewhere"
    existing = tmp_path / "other" / "elsewhere"
    planning.mkdir(parents=True)
    checkout.mkdir(parents=True)
    existing.mkdir(parents=True)
    db = get_db()
    try:
        scaffolding.link_repo_into_source(planning, existing, "elsewhere")
        register_repo_policy("p", "elsewhere", existing, pinned_branch=None, protect_base_checkout=False, db=db)
        _insert_projects(db, [("p", "Alpha", str(checkout))])
        conversion.convert_legacy_project_paths(_selection(workspace), db=db)
        policies = get_project_repo_policies("p", db=db)
    finally:
        db.close()

    assert policies["elsewhere"].path == existing
    assert policies["elsewhere-2"].path == checkout


def test_link_refusal_leaves_no_policy_and_warns(isolated_db, tmp_path, monkeypatch):
    import api.project_scaffolding as scaffolding
    import core.project_path_conversion as conversion
    from core.project_context import resolve_project_paths
    from infra.database import get_db
    from infra.project_repo_policy import get_project_repo_policies

    class CapturingLogger:
        def __init__(self) -> None:
            self.warnings: list[str] = []

        def warning(self, message: str, *args: object) -> None:
            self.warnings.append(message.format(*args))

        def info(self, _message: str, *_args: object) -> None:
            pass

    logger = CapturingLogger()
    home = tmp_path / "home"
    workspace = home / "workspace"
    planning = workspace / "projects" / "alpha"
    checkout = tmp_path / "outside" / "refused"
    planning.mkdir(parents=True)
    checkout.mkdir(parents=True)
    monkeypatch.setattr(scaffolding.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(conversion, "logger", logger)
    db = get_db()
    try:
        _insert_projects(db, [("p-refused", "Alpha", str(checkout))])
        result = conversion.convert_legacy_project_paths(_selection(workspace), db=db)
        stored_path = _stored_paths(db)["p-refused"]
        policies = get_project_repo_policies("p-refused", db=db)
        resolved = resolve_project_paths(workspace, "p-refused", db=db)
    finally:
        db.close()

    # The kept row still runs in its project folder.
    assert resolved == ("Alpha", planning.resolve())
    assert result.converted == 0
    assert result.kept_legacy == 1
    assert stored_path == str(checkout)
    assert not policies
    assert not (planning / "source" / "refused").exists()
    assert any("project_id=p-refused" in warning and str(checkout) in warning for warning in logger.warnings)


def test_policy_db_failure_rolls_back_all_conversion_rows(isolated_db, tmp_path, monkeypatch):
    import sqlite3

    import api.project_scaffolding as scaffolding
    import core.project_path_conversion as conversion
    from infra.database import get_db
    from infra.project_repo_policy import get_project_repo_policies

    monkeypatch.setattr(scaffolding.Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    for name in ("alpha", "beta"):
        (workspace / "projects" / name).mkdir(parents=True)
    bad = tmp_path / "legacy" / "bad"
    good = tmp_path / "legacy" / "good"
    bad.mkdir(parents=True)
    good.mkdir(parents=True)
    real_register = conversion.register_repo_policy
    def fail_one(project_id, *args, **kwargs):
        if project_id == "p-bad":
            raise sqlite3.IntegrityError("simulated policy write failure")
        return real_register(project_id, *args, **kwargs)

    monkeypatch.setattr(conversion, "register_repo_policy", fail_one)
    db = get_db()
    try:
        _insert_projects(db, [("p-bad", "Alpha", str(bad)), ("p-good", "Beta", str(good))])
        with pytest.raises(sqlite3.IntegrityError, match="simulated policy write failure"):
            conversion.convert_legacy_project_paths(_selection(workspace), db=db)
        stored_paths = _stored_paths(db)
        bad_policies = get_project_repo_policies("p-bad", db=db)
        good_policies = get_project_repo_policies("p-good", db=db)
    finally:
        db.close()

    assert stored_paths == {"p-bad": str(bad), "p-good": str(good)}
    assert not bad_policies
    assert not good_policies
    assert not (workspace / "projects" / "alpha" / "source" / "bad").exists()
    assert not (workspace / "projects" / "beta" / "source" / "good").exists()


def test_startup_calls_converter_after_database_initialization(tmp_path, monkeypatch):
    import app_lifecycle
    import core.project_path_conversion as conversion
    import core.skill_key_conversion as skill_key_conversion
    import core.system_projection as system_projection
    import infra.config as config
    import infra.guidance_git as guidance_git
    import infra.settings as settings
    import infra.startup_prerequisites as prerequisites
    import registry.durable_writes as durable_writes
    import registry.relocate as relocate

    calls: list[str] = []

    def record(name: str):
        return lambda *_args, **_kwargs: calls.append(name)

    for directory in ("UPLOADS_DIR", "LOGS_DIR", "RUNTIME_DIR"):
        monkeypatch.setattr(config, directory, tmp_path / directory)
    monkeypatch.setattr(prerequisites, "prepare_startup_prerequisites", record("prerequisites"))
    monkeypatch.setattr(app_lifecycle, "init_db", record("init_db"))
    monkeypatch.setattr(conversion, "convert_legacy_project_paths", record("convert"))
    monkeypatch.setattr(durable_writes, "recover_write_intents", record("write_intents"))
    monkeypatch.setattr(relocate, "recover_interrupted_relocations", record("relocations"))
    monkeypatch.setattr(settings, "bootstrap_env", record("env"))
    monkeypatch.setattr(system_projection, "ensure_system_projection", record("projection"))
    monkeypatch.setattr(skill_key_conversion, "convert_legacy_skill_keys", record("skill_keys"))
    monkeypatch.setattr(guidance_git, "startup_sweep", record("sweep"))

    class QuietLogger:
        def info(self, *_args: object) -> None:
            pass

    app_lifecycle._initialize_startup(QuietLogger())

    assert calls.index("init_db") + 1 == calls.index("convert")


def test_send_dispatch_maps_resolver_value_error_to_send_rejected(
    isolated_db,
    monkeypatch,
):
    import core.project_context as project_context
    from infra import send_dispatch
    from infra.send_types import SendRejected

    def fail(*_args, **_kwargs):
        raise ValueError("logical project selected root does not match workspace argument")

    monkeypatch.setattr(project_context, "resolve_project_paths", fail)

    with pytest.raises(SendRejected) as rejected:
        send_dispatch._validate_project_working_dir("legacy", harness="Codex")

    assert rejected.value.status_code == 422
    assert "Codex turn blocked" in str(rejected.value)
    assert rejected.value.metadata["cause"] == "project_working_directory_unresolved"
    assert isinstance(rejected.value.__cause__, ValueError)
