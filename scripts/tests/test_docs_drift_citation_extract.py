"""Tests for citation extraction (check_docs_drift.citation_extract)."""

from __future__ import annotations

from check_docs_drift import citation_extract as extract

TOP_LEVEL = {"backend", "frontend", "scripts", "docs", "context", "README.md"}


def test_path_candidate_recognized_by_first_segment():
    text = "See `backend/api/models.py` for the route table."

    assert extract.extract_path_candidates(text, TOP_LEVEL) == {"backend/api/models.py"}


def test_bare_root_file_recognized_by_whole_span():
    text = "Read `README.md` first."

    assert extract.extract_path_candidates(text, TOP_LEVEL) == {"README.md"}


def test_unknown_top_level_segment_is_not_a_path_candidate():
    text = "Install `vendor/lib/thing.js` if needed."

    assert extract.extract_path_candidates(text, TOP_LEVEL) == set()


def test_span_with_whitespace_or_leading_slash_or_url_is_excluded():
    text = "`backend api` and `/backend/api.py` and `https://backend/api` are not paths."

    assert extract.extract_path_candidates(text, TOP_LEVEL) == set()


def test_known_non_path_token_is_excluded_even_though_it_matches_a_top_level_name():
    """`qa/llm-reviewer` looks like a qa/-rooted path but is a commit-status name."""
    text = "The merge gate is `qa/llm-reviewer`."

    assert extract.extract_path_candidates(text, TOP_LEVEL | {"qa"}) == set()


def test_route_extraction_strips_an_http_method_prefix():
    text = "Call `GET /api/models/{model_id}/profile` to read a profile."

    assert extract.extract_routes(text) == {"/api/models/{model_id}/profile"}


def test_route_extraction_without_a_method_prefix():
    text = "The endpoint is `/api/status`."

    assert extract.extract_routes(text) == {"/api/status"}


def test_non_route_backtick_span_is_ignored_by_route_extraction():
    text = "See `backend/api/models.py`."

    assert extract.extract_routes(text) == set()


def test_single_span_corsia_subcommand_is_extracted_as_a_tuple():
    text = "Run `corsia service status` to check the daemon."

    assert extract.extract_corsia_subcommands(text) == {("service", "status")}


def test_single_word_corsia_subcommand_is_extracted():
    text = "Run `corsia logs` to tail output."

    assert extract.extract_corsia_subcommands(text) == {("logs",)}


def test_pipe_alternated_multi_span_citation_is_not_expanded():
    """A documented gap: `corsia start` / `stop` / `restart` yields only the first."""
    text = "Run `corsia start` / `stop` / `restart` as needed."

    assert extract.extract_corsia_subcommands(text) == {("start",)}


def test_python_version_citation_is_extracted_from_a_requirements_row():
    text = "| Python 3.11 | CI and the test suite use exactly 3.11. |"

    assert extract.extract_cited_python_version(text) == "3.11"


def test_python_version_citation_is_none_when_absent():
    text = "| Node.js 20.19+ or 22.12+ | Required by the frontend build. |"

    assert extract.extract_cited_python_version(text) is None
