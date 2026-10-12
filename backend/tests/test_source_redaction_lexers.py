"""Source-path redaction for the Pygments-lexed extensions (corsia#551).

Credential-shaped literals are built by concatenation, so this file holds none
and reads of it stay faithful.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path

import pytest
from pygments.lexers import (
    BashLexer, JavascriptLexer, JsonLexer, MarkdownLexer, TOMLLexer, TsxLexer,
    TypeScriptLexer, YamlLexer,
)
from pygments.token import Comment, Error, Keyword, Name, Operator, Punctuation, String, Text

from host_access import source_redaction
from host_access.credential_sanitizer import (
    REDACTION_MARKER,
    credential_spans,
    sanitize_credential_text,
)
from host_access.source_redaction import FALLBACKS, sanitize_source


_API_KEY = "api" + "_key"
_APIKEY = "api" + "Key"
_PASSWORD = "pass" + "word"
_TOKEN = "tok" + "en"
_PEM_BEGIN = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
_PEM_END = "-" * 5 + "END PRIVATE KEY" + "-" * 5
_SCHEME = "https" + ":" + "//"
_VENDOR = (
    "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K",
    "sk" + "-ant-api03-" + "AbCdefghijklmnopqrstu",
    "AKIA" + "ABCDEFGHIJKLMNOP",
)
_REPO = Path(__file__).resolve().parents[2]


def _safe(content: str, suffix: str) -> str:
    result = sanitize_source(content, suffix)
    assert result is not None, suffix
    return result


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        # The 2026-10-09 probe: types after credential-named fields and parameters.
        (".ts", (
            f"interface Settings {{\n  {_APIKEY}: string | null;\n  {_TOKEN}?: string;\n}}\n"
            f"const {_API_KEY}: string = getKey(user);\n"
            f"function load({_PASSWORD}: string): void {{}}\n"
            f"// {_TOKEN}: docs\n"
        )),
        (".tsx", f"const {_APIKEY}: string = load();\nconst view = <Field {_TOKEN}={{value}} />;\n"),
        (".js", f"const {_API_KEY} = getKey(user);\n// {_TOKEN}: docs\n"),
        (".yaml", f"db:\n  {_PASSWORD}: *shared\n  {_API_KEY}:\n    from: env\n# {_TOKEN}: docs\n"),
        (".sh", (
            f'curl -H "Authorization: Bearer ${_TOKEN.upper()}" "$URL"\n'
            f"echo ${_TOKEN.upper()}\n# {_TOKEN}: docs\n"
        )),
        (".md", (
            f"Set the {_API_KEY}: field in Settings.\n\n"
            f"```ts\nconst {_APIKEY}: string | null = load();\n```\n"
            f"<!-- {_TOKEN}: docs -->\n"
        )),
        (".json", f'{{"{_TOKEN}_type": "bearer", "{_TOKEN}": null, "enabled": true}}\n'),
        (".toml", f'[auth]\n{_TOKEN}_url = "{_SCHEME}example.test/auth"\n# {_TOKEN}: docs\n'),
    ],
)
def test_code_types_and_prose_stay_as_written(suffix, content):
    assert _safe(content, suffix) == content


@pytest.mark.parametrize(
    ("suffix", "content", "secrets"),
    [
        (".ts", f'const {_APIKEY} = "{_VENDOR[0]}";\nconst other = {_VENDOR[1]};\n', _VENDOR[:2]),
        (".ts", f'const c = {{ {_APIKEY}: "abc123def" }};\n{_APIKEY} = "abc123def";\n', ("abc123def",)),
        (".ts", f"const k = `\n{_PEM_BEGIN}\nbody-line-one\n{_PEM_END}\n`;\n", ("body-line-one",)),
        (".yaml", (
            f"{_PASSWORD}: hunter2\n{_API_KEY}: 'abc123def'\nother: {_VENDOR[2]}\n"
            f"key: |\n  {_PEM_BEGIN}\n  body-line-one\n  {_PEM_END}\n"
        ), ("hunter2", "abc123def", _VENDOR[2], "body-line-one")),
        (".toml", f'{_PASSWORD} = "abc123def"\nother = "{_VENDOR[0]}"\n', ("abc123def", _VENDOR[0])),
        (".json", f'{{"{_PASSWORD}": "abc123def", "{_TOKEN}": 123456}}\n', ("abc123def", "123456")),
        (".sh", (
            f"{_API_KEY.upper()}=abc123def\nexport {_TOKEN.upper()}=hunter2\nrun {_VENDOR[1]}\n"
        ), ("abc123def", "hunter2", _VENDOR[1])),
        (".md", (
            f"The {_API_KEY}: abc123 here and {_TOKEN}: Hunter there.\n"
            f"Use {_VENDOR[0]} today.\n"
        ), ("abc123", "Hunter", _VENDOR[0])),
        # URL userinfo is never code or prose: a link target lexes as a name.
        (".md", (
            f"See [docs]({_SCHEME}user:hunter@example.test/a) or {_SCHEME}user:hunter@example.test/b\n"
        ), ("hunter",)),
        # A credential inside one name token: the text rules change the token itself.
        (".md", f"See [docs]({_SCHEME}example.test/?{_TOKEN}=abc123def)\n", ("abc123def",)),
        # A cookie value is never a prose word.
        (".md", "Send Cookie: session=hunter with it.\n", ("hunter",)),
    ],
)
def test_credential_values_are_redacted(suffix, content, secrets):
    safe = _safe(content, suffix)

    for secret in secrets:
        assert secret not in safe
    assert REDACTION_MARKER in safe
    assert len(safe.splitlines()) == len(content.splitlines())


@pytest.mark.parametrize(
    ("suffix", "content", "reason"),
    [
        (".ts", f'const {_APIKEY} = "abc123def";\nconst x = #;\n', "error_token"),
        (".yaml", f"a:\n\t- b\n{_PASSWORD}: abc123def\n", "error_token"),
        # Pygments rewrites CR and CRLF before lexing, so the file cannot round-trip.
        (".ts", f'const {_APIKEY} = "abc123def";\r\nconst y = 1;\r\n', "roundtrip"),
    ],
)
def test_unproven_files_take_the_text_path_and_are_counted(suffix, content, reason, caplog):
    before = FALLBACKS[reason]
    with caplog.at_level(logging.INFO, logger=source_redaction.__name__):
        assert sanitize_source(content, suffix) is None

    assert FALLBACKS[reason] == before + 1
    assert "abc123def" not in sanitize_credential_text(content)
    # The log line names the reason only: no content, no path.
    assert reason in caplog.text
    assert "abc123def" not in caplog.text


def test_size_and_line_count_fallbacks_are_counted(monkeypatch):
    content = f'const {_APIKEY} = "abc123def";\nconst y = 1;\n'
    size, linecount = FALLBACKS["size"], FALLBACKS["linecount"]

    monkeypatch.setattr(source_redaction, "MAX_LEXED_CHARS", 10)
    assert sanitize_source(content, ".ts") is None
    monkeypatch.undo()
    monkeypatch.setattr(source_redaction, "_BREAK_RE", re.compile(r"(?!)"))
    multi_line = f"const k = `{_PEM_BEGIN}\nbody-one\nbody-two\n{_PEM_END}`;\n"
    assert sanitize_source(multi_line, ".ts") is None

    assert FALLBACKS["size"] == size + 1
    assert FALLBACKS["linecount"] == linecount + 1


@pytest.mark.parametrize(
    ("suffix", "line"),
    [
        (".ts", f"const {_API_KEY}: string = load()"),
        (".yaml", f"{_PASSWORD}: *shared"),
        (".md", f"The {_API_KEY}: field here."),
        (".sh", f"{_TOKEN.upper()}=$OTHER"),
    ],
)
def test_leading_blank_lines_and_no_final_newline_stay_on_the_source_path(suffix, line):
    for content in (f"\n\n{line}", line, f"\n{line}\n\n"):
        assert _safe(content, suffix) == content


def test_python_keeps_lone_cr_faithful_through_the_merged_module():
    content = f"{_API_KEY}: str\rvalue = 1\r{_API_KEY.upper()} = \"abc123def\"\r"

    safe = _safe(content, ".py")

    assert safe == content.replace("abc123def", REDACTION_MARKER)


def test_extensions_outside_the_closed_map_keep_the_text_path():
    content = f"{_API_KEY}: abc123def\n"
    for suffix in (".env", ".txt", "", ".svelte", ".PYC"):
        assert sanitize_source(content, suffix) is None


def test_the_lexer_map_names_explicit_classes():
    assert source_redaction._LEXERS == {
        ".ts": TypeScriptLexer, ".tsx": TsxLexer, ".js": JavascriptLexer, ".jsx": TsxLexer,
        ".mjs": JavascriptLexer, ".cjs": JavascriptLexer, ".sh": BashLexer,
        ".bash": BashLexer, ".zsh": BashLexer, ".yaml": YamlLexer, ".yml": YamlLexer,
        ".json": JsonLexer, ".toml": TOMLLexer, ".md": MarkdownLexer,
    }


# Token kinds on fixed samples. A Pygments upgrade that changes any of them
# changes what stays visible; review the new kinds against the keep rules
# before updating a digest.
_DRIFT_SAMPLES = {
    TypeScriptLexer: (
        f"function load({_PASSWORD}: string): void {{}}\n"
        f"const k = `{_TOKEN}=abc${{x}}def`; // {_TOKEN}: docs\n"
    ),
    TsxLexer: f"const v = <Field {_TOKEN}={{value}} label=\"x\" />;\n",
    JavascriptLexer: f'const {_API_KEY} = "abc"; /* {_TOKEN}: docs */\n',
    BashLexer: f'export {_TOKEN.upper()}=abc\ncurl -H "Authorization: Bearer ${_TOKEN.upper()}"\n',
    YamlLexer: f"db:\n  {_PASSWORD}: hunter\n  {_API_KEY}: 'abc'\n  {_TOKEN}: *ref\n# {_TOKEN}: docs\n",
    JsonLexer: f'{{"{_PASSWORD}": "abc", "{_TOKEN}": 123, "n": true}}\n',
    TOMLLexer: f'{_PASSWORD} = "abc"\n# {_TOKEN}: docs\n',
    MarkdownLexer: (
        f"The {_API_KEY}: field. [x]({_SCHEME}h.test/?{_TOKEN}=abc)\n\n"
        f"```ts\nconst {_APIKEY}: string = load();\n```\n"
    ),
}
_DRIFT_DIGESTS = {
    "TypeScriptLexer": "51e2fd4dd463ba1b54e9ea627e8fd2e1ced87acdbbf7f0f26fbd2104d35368f4",
    "TsxLexer": "ffa9bf1719884df2efee6f34928dc09bcbe63fbe8f24faccfe7d5d030520f277",
    "JavascriptLexer": "5f55be6ed02c70cbcb13f051018d24c13de5cac6a1bf1dbe5458ba00191e97db",
    "BashLexer": "c7c2583190f9afb9445ca0093b3b4a9c635c3c105526ab921a54ba4f0c773222",
    "YamlLexer": "2d20b0bcb066465f41e65f84cd10d2fe84c1f57693d98252052d2a050f2f332e",
    "JsonLexer": "6d4723f47999851067f0605ac7559ad32d66b5f90d1ea1ee06d01f663c522b5e",
    "TOMLLexer": "af4735ac7a88b40a5f8849a6b788a8259c8f9477e94981e7c4c0e52475831093",
    "MarkdownLexer": "974df5e7dde250bac6506ed57233d6833a45086491235160fe7d5bda3fbcd03e",
}


def _kind_digest(lexer_class, content: str) -> str:
    stream = "\n".join(
        f"{kind}\t{value!r}"
        for kind, value in lexer_class(stripnl=False, ensurenl=False).get_tokens(content)
    )
    return hashlib.sha256(stream.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("lexer_class", list(_DRIFT_SAMPLES))
def test_token_kinds_have_not_drifted(lexer_class):
    assert _kind_digest(lexer_class, _DRIFT_SAMPLES[lexer_class]) == _DRIFT_DIGESTS[
        lexer_class.__name__
    ]


def _corpus() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "frontend/src/*.ts", "frontend/src/*.tsx", "*.yaml", "*.md"],
        cwd=_REPO, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    paths = [_REPO / name for name in listed.split("\0") if name]
    assert paths, "corpus is empty"
    return [path for path in paths if path.is_file()]


def _mask(length: int, ranges) -> bytearray:
    mask = bytearray(length)
    for start, end in ranges:
        mask[start:end] = b"\x01" * (end - start)
    return mask


def _expected(content: str, suffix: str) -> str:
    """The keep rule, derived independently of the module's offsets and bisects."""
    lexer_class = source_redaction._LEXERS[suffix]
    never = _mask(len(content), credential_spans(content, never_code=True))
    kept, prose = bytearray(len(content)), bytearray(len(content))
    offset = 0
    for kind, value in lexer_class(stripnl=False, ensurenl=False).get_tokens(content):
        start, offset = offset, offset + len(value)
        if any(kind in group for group in (Name, Keyword, Operator, Punctuation)):
            if not any(never[start:offset]) and sanitize_credential_text(value) == value:
                kept[start:offset] = b"\x01" * len(value)
        elif kind in String and value[:1] in "\"'`":
            quote = value[0]
            if value.strip(quote) == "":
                kept[start:offset] = b"\x01" * len(value)
            else:
                width = 3 if len(value) >= 6 and value[:3] == value[-3:] == quote * 3 else 1
                if len(value) >= 2 and value.endswith(quote * width):
                    kept[start:start + width] = b"\x01" * width
                    kept[offset - width:offset] = b"\x01" * width
        elif kind in Comment or (lexer_class is MarkdownLexer and kind in Text):
            prose[start:offset] = b"\x01" * len(value)
    spans = credential_spans(content)
    for start, end in spans:
        if (
            re.fullmatch(r"[a-z]{1,15}[.,;:]?", content[start:end])
            and all(prose[start:end]) and not any(never[start:end])
        ):
            kept[start:end] = b"\x01" * (end - start)
    redacted = bytes(
        1 if inside and not keep else 0
        for inside, keep in zip(_mask(len(content), spans), kept, strict=True)
    )
    pieces, cursor = [], 0
    for run in re.finditer(b"\x01+", redacted):
        pieces.append(content[cursor:run.start()])
        breaks = source_redaction._BREAK_RE.findall(content[run.start():run.end()])
        pieces.append(REDACTION_MARKER + "".join(breaks))
        cursor = run.end()
    pieces.append(content[cursor:])
    return "".join(pieces)


def test_corpus_keeps_line_counts_and_the_keep_rule():
    reasons: dict[str, int] = {}
    for path in _corpus():
        content = path.read_text(encoding="utf-8")
        before = dict(FALLBACKS)
        safe = sanitize_source(content, path.suffix)
        if safe is None:
            changed = {key for key, value in FALLBACKS.items() if value != before.get(key, 0)}
            # Every fallback is counted by its reason, never silent.
            assert changed, path
            assert changed <= {"error_token", "roundtrip", "size"}, (path, changed)
            for key in changed:
                reasons[key] = reasons.get(key, 0) + 1
            continue
        assert len(safe.splitlines()) == len(content.splitlines()), path
        assert safe.count("\n") == content.count("\n"), path
        assert safe == _expected(content, path.suffix.casefold()), path
        for start, end in credential_spans(content, shape_only=True):
            secret = content[start:end]
            assert safe.count(secret) < content.count(secret), path
    # Fallbacks stay rare: a lexer that rejected much of the corpus would
    # quietly undo this change.
    assert sum(reasons.values()) <= 20, reasons


@pytest.mark.asyncio
async def test_read_tool_returns_typescript_faithfully_and_unproven_files_redacted(
    tmp_path, monkeypatch,
):
    from host_access.credential_sanitizer import CredentialSafeText
    from tools import sandbox_config
    from tools.filesystem_rw import ReadFileTool
    from tools.registry import ToolRegistry

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [tmp_path])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    probe = tmp_path / "probe.ts"
    probe.write_text(f"const {_API_KEY}: string = getKey(user);\n", encoding="utf-8")
    crlf = tmp_path / "crlf.ts"
    crlf.write_bytes(f'const {_APIKEY} = "abc123def";\r\n'.encode("utf-8"))
    reader = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    registry = ToolRegistry()
    registry.register(reader)

    async def _context(self):
        return {"id": "thread-1", "project_id": None}, None

    async def _reminder(result, _name, _params):
        return result

    monkeypatch.setattr(ToolRegistry, "_load_thread_context", _context)
    monkeypatch.setattr("tools.registry.remind_current_turn", _reminder)

    assert isinstance(await reader.execute(str(probe)), CredentialSafeText)
    shown = await registry.execute("read_file", {"path": str(probe)})
    redacted = await registry.execute("read_file", {"path": str(crlf)})

    assert shown == f"     1\tconst {_API_KEY}: string = getKey(user);"
    assert "abc123def" not in redacted
    assert REDACTION_MARKER in redacted
