"""Tests for citation existence verification (check_docs_drift.citation_verify)."""

from __future__ import annotations

from check_docs_drift import citation_verify as verify


def test_missing_paths_finds_a_path_that_exists_nowhere(tmp_path):
    (tmp_path / "backend").mkdir()

    missing = verify.missing_paths({"backend/nope.py"}, tmp_path)

    assert missing == ["backend/nope.py"]


def test_missing_paths_accepts_a_path_resolved_directly(tmp_path):
    real = tmp_path / "backend" / "api" / "models.py"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")

    assert verify.missing_paths({"backend/api/models.py"}, tmp_path) == []


def test_missing_paths_checks_the_fallback_dir_before_reporting_missing(tmp_path):
    """A bare `infra/x.py` citation resolves against `backend/infra/x.py`."""
    real = tmp_path / "backend" / "infra" / "thread_inputs.py"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")

    missing = verify.missing_paths({"infra/thread_inputs.py"}, tmp_path, fallback_dir="backend")

    assert missing == []


def test_missing_paths_still_reports_missing_when_absent_from_both_locations(tmp_path):
    (tmp_path / "backend").mkdir()

    missing = verify.missing_paths({"infra/ghost.py"}, tmp_path, fallback_dir="backend")

    assert missing == ["infra/ghost.py"]


def _write_api_file(api_dir, name: str, text: str) -> None:
    (api_dir / name).write_text(text, encoding="utf-8")


def test_registered_routes_combine_own_prefix_and_decorators(tmp_path):
    api_dir = tmp_path / "backend" / "api"
    api_dir.mkdir(parents=True)
    _write_api_file(
        api_dir,
        "models.py",
        'router = APIRouter(prefix="/api/models")\n\n'
        '@router.get("/{model_id}")\n'
        "def get_model(): ...\n",
    )

    routes = verify.registered_api_routes(api_dir)

    assert routes == {"/api/models/{}"}


def test_registered_routes_include_a_helper_that_inherits_the_callers_prefix(tmp_path):
    """A route helper without a router inherits the caller's mounted prefix."""
    api_dir = tmp_path / "backend" / "api"
    api_dir.mkdir(parents=True)
    _write_api_file(
        api_dir,
        "models.py",
        'router = APIRouter(prefix="/api/models")\n\n'
        "register_model_profile_routes(router)\n",
    )
    _write_api_file(
        api_dir,
        "model_profiles.py",
        "def register_model_profile_routes(router: APIRouter):\n"
        '    @router.get("/{model_id}/profile")\n'
        "    def get_profile(): ...\n",
    )

    routes = verify.registered_api_routes(api_dir)

    assert "/api/models/{}/profile" in routes


def test_missing_routes_excludes_known_non_corsia_routes(tmp_path):
    api_dir = tmp_path / "backend" / "api"
    api_dir.mkdir(parents=True)
    _write_api_file(api_dir, "models.py", 'router = APIRouter(prefix="/api/models")\n')

    missing = verify.missing_routes({"/api/status"}, api_dir)

    assert missing == []


def test_missing_routes_reports_a_route_with_no_matching_decorator(tmp_path):
    api_dir = tmp_path / "backend" / "api"
    api_dir.mkdir(parents=True)
    _write_api_file(api_dir, "models.py", 'router = APIRouter(prefix="/api/models")\n')

    missing = verify.missing_routes({"/api/models/ghost"}, api_dir)

    assert missing == ["/api/models/ghost"]


def test_corsia_case_labels_splits_pipe_alternated_labels(tmp_path):
    script = tmp_path / "corsia"
    script.write_text(
        "case \"$1\" in\n"
        "  service)\n"
        "    case \"$2\" in\n"
        "      install|uninstall|start|stop|restart|status)\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "  user|token)\n"
        "    python -m cli \"$@\"\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )

    labels = verify.corsia_case_labels(script)

    assert {"service", "install", "start", "status", "user", "token"} <= labels


def test_missing_subcommands_checks_only_the_first_word_for_non_nested_commands(tmp_path):
    """`corsia user add` forwards to a different program's own parser."""
    script = tmp_path / "corsia"
    script.write_text("case \"$1\" in\n  user|token)\n    ;;\nesac\n", encoding="utf-8")

    missing = verify.missing_subcommands({("user", "add")}, script)

    assert missing == []


def test_missing_subcommands_checks_both_words_for_nested_dispatch_commands(tmp_path):
    """`service` really does nest a second case, so an unknown second word is real drift."""
    script = tmp_path / "corsia"
    script.write_text(
        "case \"$1\" in\n"
        "  service)\n"
        "    case \"$2\" in\n"
        "      start|stop)\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )

    missing = verify.missing_subcommands({("service", "ghost")}, script)

    assert missing == ["corsia service ghost"]


def test_missing_subcommands_reports_an_unknown_first_word(tmp_path):
    script = tmp_path / "corsia"
    script.write_text("case \"$1\" in\n  service)\n    ;;\nesac\n", encoding="utf-8")

    missing = verify.missing_subcommands({("ghost",)}, script)

    assert missing == ["corsia ghost"]
