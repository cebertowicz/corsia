"""Grep content lines show what ``read_file`` shows for the same line (corsia#551).

Each search path is covered: ripgrep, the Python fallback taken without
ripgrep, and the reviewed host-path search. Credential-shaped literals are
built by concatenation, so this file holds none and reads of it stay faithful.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from host_access.credential_sanitizer import CredentialSafeText, sanitize_credential_text
from host_access.source_redaction import FALLBACKS
from tools.filesystem_rw import ReadFileTool
from tools.grep import GrepTool
import tools.grep as grep_mod

_CUR = "thread-CUR"
_APIKEY = "api" + "Key"
_API_KEY = "api" + "_key"
_TOKEN = "tok" + "en"
_VENDOR = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K"
_PEM_BODY = "MIIEvQ" + "IBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
_SCHEME = "https" + ":" + "//"
_USERINFO_SECRET = "hunter" + "2pass"
_SEARCHES = ("ripgrep", "python", "reviewed")

_TS = (
    "export interface Settings {\n"
    f"  {_APIKEY}: string | null;\n"
    "}\n"
    f"const {_APIKEY}Value: string = getKey(user);\n"
    f'const fallback = "{_VENDOR}";\n'
)
_PY = (
    f"def load({_API_KEY}: str) -> None:\n"
    f"    {_TOKEN}: str = fetch()\n"
    f'    fallback = "{_VENDOR}"\n'
    'KEY = """\n'
    + "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5 + "\n"
    + f"{_PEM_BODY}\n"
    + "-" * 5 + "END PRIVATE KEY" + "-" * 5 + "\n"
    '"""\n'
)
_MD = (
    f"Set the {_API_KEY}: field in Settings.\n"
    f"See [docs]({_SCHEME}admin:{_USERINFO_SECRET}@example.test/path) first.\n"
)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    from tools import sandbox_config

    ws = tmp_path / "ws"
    files = {
        ws / "src" / "settings.ts": _TS,
        ws / "pkg" / "module.py": _PY,
        ws / "docs" / "guide.md": _MD,
        ws / "notes" / "plain.txt": f'fallback = "{_VENDOR}"\n',
        ws / "a-1:b" / "x:y-2.ts": _TS,
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(sandbox_config, "WORKSPACE", ws)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ws])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    return ws


async def _grep(ws: Path, search: str, monkeypatch, pattern: str, *, context: int = 0,
                output_mode: str = "content", glob: str | None = None) -> str:
    tool = GrepTool(workspace=ws)
    if search == "reviewed":
        return tool._search_reviewed(
            pattern, ws, glob, output_mode, False, context, 50, _CUR, False,
        )
    if search == "ripgrep" and not grep_mod._RG_AVAILABLE:
        pytest.skip("ripgrep not available")
    monkeypatch.setattr(grep_mod, "_RG_AVAILABLE", search == "ripgrep")
    return await tool.execute(
        pattern=pattern, output_mode=output_mode, context=context, glob=glob,
        _thread_id=_CUR,
    )


async def _read_lines(ws: Path, relative: str) -> list[str]:
    """Each line's text, without ``read_file``'s line-number column."""
    reader = ReadFileTool(workspace=ws, allowed_dir=ws)
    rendered = str(await reader.execute(str(ws / relative))).splitlines()
    return [line.split("\t", 1)[1] for line in rendered]


def _parsed(output: str, relative: str, search: str) -> list[tuple[str, int, str]]:
    """``(separator, number, text)`` for each output line of one file."""
    tail = re.compile(r"(?P<sep>[:-])(?P<number>[0-9]+)(?P=sep)" + (" " if search == "python" else ""))
    rows = []
    for line in output.splitlines():
        if not line.startswith(relative):
            continue
        match = tail.match(line, len(relative))
        assert match is not None, line
        rows.append((match.group("sep"), int(match.group("number")), line[match.end():]))
    return rows


def _delta(before: Counter[str]) -> Counter[str]:
    return Counter({reason: FALLBACKS[reason] - before[reason] for reason in FALLBACKS}) - Counter()


@pytest.mark.asyncio
@pytest.mark.parametrize("search", _SEARCHES)
@pytest.mark.parametrize("relative", ["src/settings.ts", "pkg/module.py", "a-1:b/x:y-2.ts"])
async def test_a_match_line_equals_the_same_line_of_read_file(workspace, monkeypatch, search, relative):
    pattern = f"{_APIKEY}|{_API_KEY}|{_TOKEN}|fallback|MIIEvQ"
    output = await _grep(workspace, search, monkeypatch, pattern)
    read = await _read_lines(workspace, relative)
    rows = _parsed(output, relative, search)

    assert rows, output
    for _sep, number, text in rows:
        expected = read[number - 1].rstrip() if search == "python" else read[number - 1]
        assert text == expected
    # The types are shown as written; the values are not shown at all.
    assert ": string | null;" in output or relative.endswith(".py")
    assert f"{_TOKEN}: str = fetch()" in output or not relative.endswith(".py")
    assert _VENDOR not in output
    assert _PEM_BODY not in output
    assert isinstance(output, CredentialSafeText)


@pytest.mark.asyncio
@pytest.mark.parametrize("search", ["ripgrep", "reviewed"])
async def test_context_lines_and_separators_are_faithful(workspace, monkeypatch, search):
    relative = "a-1:b/x:y-2.ts"
    output = await _grep(workspace, search, monkeypatch, "interface|fallback", context=1)
    read = await _read_lines(workspace, relative)
    rows = _parsed(output, relative, search)

    assert [(sep, number) for sep, number, _text in rows] == [
        (":", 1), ("-", 2), ("-", 4), (":", 5),
    ]
    assert all(text == read[number - 1] for _sep, number, text in rows)
    assert f"\n--\n{relative}-4-" in output
    assert _VENDOR not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("search", _SEARCHES)
async def test_unsubstituted_lines_are_text_sanitized_before_the_result_is_marked_safe(
    workspace, monkeypatch, search,
):
    output = await _grep(workspace, search, monkeypatch, "fallback|admin")

    assert isinstance(output, CredentialSafeText)
    assert _VENDOR not in output
    # R1: a link's userinfo is never kept as code, though the prose around it is.
    assert _USERINFO_SECRET not in output
    assert "See [docs](" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("search", _SEARCHES)
async def test_a_crlf_source_line_is_still_redacted(workspace, monkeypatch, search):
    crlf = workspace / "src" / "windows.ts"
    crlf.write_bytes(_TS.replace("\n", "\r\n").encode("utf-8"))

    output = await _grep(workspace, search, monkeypatch, "fallback")

    assert "windows.ts" in output
    assert _VENDOR not in output


@pytest.mark.asyncio
async def test_a_line_changed_after_the_search_falls_back_and_is_redacted(workspace, monkeypatch):
    if not grep_mod._RG_AVAILABLE:
        pytest.skip("ripgrep not available")
    changed = _TS.replace(f'"{_VENDOR}"', '"rotated"')
    monkeypatch.setattr("tools.grep_ripgrep.read_source", lambda _path: changed)
    before = Counter(FALLBACKS)

    output = await _grep(workspace, "ripgrep", monkeypatch, "fallback", glob="settings.ts")

    assert "src/settings.ts:5:" in output
    assert _VENDOR not in output
    assert "rotated" not in output
    assert _delta(before)["grep_line_mismatch"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("search", _SEARCHES)
async def test_past_the_budget_lines_take_the_text_rules_and_are_counted(
    workspace, monkeypatch, search,
):
    monkeypatch.setattr("tools.grep_content.BUDGET_SECONDS", 0.0)
    before = Counter(FALLBACKS)

    output = await _grep(workspace, search, monkeypatch, "fallback")

    assert output.count("fallback") == 4
    assert _VENDOR not in output
    # One per source-path line: two .ts and one .py; the .txt never had a source path.
    assert _delta(before)["grep_budget"] == 3
    assert output == CredentialSafeText(sanitize_credential_text(output))


@pytest.mark.asyncio
@pytest.mark.parametrize("search", _SEARCHES)
async def test_files_and_count_modes_are_unchanged(workspace, monkeypatch, search):
    files = await _grep(workspace, search, monkeypatch, "fallback", output_mode="files")
    counts = await _grep(workspace, search, monkeypatch, "fallback", output_mode="count")
    paths = ["a-1:b/x:y-2.ts", "notes/plain.txt", "pkg/module.py", "src/settings.ts"]

    assert sorted(files.splitlines()) == paths
    assert sorted(counts.splitlines()) == [f"{path}:1" for path in paths]
    # These still take the registry's text rules.
    assert not isinstance(files, CredentialSafeText)
    assert not isinstance(counts, CredentialSafeText)


@pytest.mark.asyncio
async def test_the_registry_passes_faithful_grep_lines_through(workspace, monkeypatch):
    from tools.registry import ToolRegistry

    if not grep_mod._RG_AVAILABLE:
        pytest.skip("ripgrep not available")
    registry = ToolRegistry()
    registry.register(GrepTool(workspace=workspace))

    async def _context(self):
        return {"id": _CUR, "project_id": None}, None

    async def _reminder(result, _name, _params):
        return f"{result}\nreminder"

    monkeypatch.setattr(ToolRegistry, "_load_thread_context", _context)
    monkeypatch.setattr("tools.registry.remind_current_turn", _reminder)
    output = await registry.execute(
        "grep", {"pattern": f"{_APIKEY}|fallback", "glob": "settings.ts", "output_mode": "content"},
    )

    assert f"src/settings.ts:2:  {_APIKEY}: string | null;" in output
    assert _VENDOR not in output
