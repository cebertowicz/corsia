"""Tests for `raw-api-request` rule."""

from __future__ import annotations

_RULE = "raw-api-request"
_PATH = "frontend/src/lib/example.ts"
_REQUEST = "frontend/src/lib/api/request.ts"


def _run(run_engine, source, path=_PATH):
    return run_engine(path, source, rule_id=_RULE)


def test_planted_raw_fetch_to_api_fires(run_engine):
    """The violation this rule exists for: a raw `/api` fetch outside `apiFetch`."""
    src = (
        "export async function loadThing() {\n"
        "\tconst response = await fetch('/api/thing');\n"
        "\treturn response.json();\n"
        "}\n"
    )
    findings = _run(run_engine, src)
    assert [f.line for f in findings] == [2]
    assert findings[0].severity == "error"
    assert "apiFetch" in (findings[0].fix_hint or "")


def test_raw_fetch_with_credential_header_still_fires(run_engine):
    """Setting the header by hand is the same bypass: one place sets the session."""
    src = (
        "export const load = (session: string) =>\n"
        "\tfetch('/api/thing', { headers: { Authorization: `Bearer ${session}` } });\n"
    )
    assert [f.line for f in _run(run_engine, src)] == [2]


def test_template_address_with_base_prefix_fires(run_engine):
    src = "const r = await fetch(`${BASE}/api/models/${id}`, { method: 'PUT' });\n"
    assert len(_run(run_engine, src)) == 1


def test_window_and_globalthis_fetch_fire(run_engine):
    src = (
        "await window.fetch('/api/a');\n"
        "await globalThis.fetch('/api/b');\n"
    )
    assert [f.line for f in _run(run_engine, src)] == [1, 2]


def test_address_held_in_a_same_file_constant_fires(run_engine):
    src = (
        "const PROGRESS = '/api/setup/progress';\n"
        "export const read = () => fetch(PROGRESS);\n"
    )
    assert [f.line for f in _run(run_engine, src)] == [2]


def test_address_built_by_a_same_file_function_fires(run_engine):
    src = (
        "function historyUrl(path: string): string {\n"
        "\treturn `/api/files/history?path=${path}`;\n"
        "}\n"
        "export const read = (p: string) => fetch(historyUrl(p));\n"
    )
    assert [f.line for f in _run(run_engine, src)] == [4]


def test_event_source_to_api_fires(run_engine):
    src = "const stream = new EventSource('/api/events');\n"
    findings = _run(run_engine, src)
    assert len(findings) == 1
    assert "EventSource" in findings[0].message


def test_xhr_open_to_api_fires(run_engine):
    src = (
        "const xhr = new XMLHttpRequest();\n"
        "xhr.open('POST', '/api/upload');\n"
    )
    findings = _run(run_engine, src)
    assert [f.line for f in findings] == [2]
    assert "XMLHttpRequest" in findings[0].message


def test_svelte_script_fires(run_engine):
    src = (
        "<script lang=\"ts\">\n"
        "\tasync function go() { await fetch('/api/x', { method: 'POST' }); }\n"
        "</script>\n"
        "<button onclick={go}>Go</button>\n"
    )
    assert [f.line for f in _run(run_engine, src, "frontend/src/lib/components/Go.svelte")] == [2]


def test_apifetch_is_not_flagged(run_engine):
    src = (
        "import { apiFetch } from '$lib/api/request';\n"
        "export const load = () => apiFetch('/api/thing');\n"
    )
    assert _run(run_engine, src) == []


def test_fetch_of_a_parameter_is_not_flagged(run_engine):
    """`imageFailure.ts` asks the same address an `<img>` asked: the address is a parameter."""
    src = (
        "export async function imageFailureReason(src: string): Promise<string> {\n"
        "\ttry {\n"
        "\t\tconst res = await fetch(src);\n"
        "\t\tvoid res.body?.cancel();\n"
        "\t} catch { /* offline */ }\n"
        "\treturn 'Couldn\\'t load this image';\n"
        "}\n"
    )
    assert _run(run_engine, src, "frontend/src/lib/imageFailure.ts") == []


def test_the_real_image_failure_file_is_not_flagged(run_engine):
    from pathlib import Path

    real = Path(__file__).resolve().parents[3] / "frontend/src/lib/imageFailure.ts"
    assert "fetch(src)" in real.read_text(encoding="utf-8")
    assert _run(run_engine, real.read_text(encoding="utf-8"), "frontend/src/lib/imageFailure.ts") == []


def test_other_origin_fetch_is_not_flagged(run_engine):
    src = "const r = await fetch('https://example.test/data.json');\n"
    assert _run(run_engine, src) == []


def test_mentions_in_comments_and_strings_are_not_flagged(run_engine):
    src = (
        "// fetch('/api/thing') was replaced by apiFetch\n"
        "/* await fetch('/api/other'); */\n"
        "export const note = \"fetch('/api/x')\";\n"
    )
    assert _run(run_engine, src) == []


def test_the_shared_request_function_is_allowlisted(run_engine):
    src = (
        "export function apiFetch(input: string, init?: RequestInit) {\n"
        "\treturn fetch(input, init);\n"
        "}\n"
        "const probe = () => fetch('/api/probe');\n"
    )
    assert _run(run_engine, src, _REQUEST) == []
    # Outside the allowlist only the literal `/api` call is flagged; `fetch(input, init)` is a parameter.
    assert [f.line for f in _run(run_engine, src)] == [4]


def test_tests_are_skipped(run_engine):
    src = "await fetch('/api/thing');\n"
    assert _run(run_engine, src, "frontend/src/lib/example.test.ts") == []
    assert _run(run_engine, src, "frontend/src/lib/__tests__/example.ts") == []


def test_paths_outside_the_frontend_are_skipped(run_engine):
    src = "await fetch('/api/thing');\n"
    assert _run(run_engine, src, "frontend/e2e/example.ts") == []
    assert _run(run_engine, src, "desktop/scripts/example.ts") == []


def test_python_files_are_skipped(run_engine):
    assert _run(run_engine, "fetch('/api/x')\n", "frontend/src/lib/example.py") == []
