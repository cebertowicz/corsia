from __future__ import annotations

import io
import re
from pathlib import Path
import tokenize
import zlib

import pytest

from host_access.credential_sanitizer import (
    REDACTION_MARKER,
    credential_spans,
    sanitize_credential_text,
)
from host_access import source_redaction as python_source_redaction


def sanitize_python_source(content: str) -> str | None:
    return python_source_redaction.sanitize_source(content, ".py")


def _safe(source: str) -> str:
    result = sanitize_python_source(source)
    assert result is not None
    return result


def test_credential_values_are_redacted_and_code_around_them_is_kept():
    source = '''\
API_KEY = "assigned-value"
Client(api_key="keyword-value", timeout=3)
count = 32
def configure(password="default-value"):
    return password
left, token = "left", "tuple-value"
os.environ["API_KEY"] = "subscript-value"
os.getenv("GITHUB_TOKEN", "getenv-default")
'''

    safe = _safe(source)

    for value in ("assigned-value", "keyword-value", "default-value"):
        assert value not in safe
    assert 'API_KEY = "[REDACTED]"' in safe
    assert 'Client(api_key="[REDACTED]", timeout=3)' in safe
    assert "count = 32" in safe
    # Literals the text rules never matched are shown, exactly as on the text
    # path: a tuple-unpacked value, a subscript target and a getenv default.
    for shown in ("tuple-value", "subscript-value", "getenv-default"):
        assert shown in safe


def test_dict_and_mapping_pattern_keys_that_are_not_string_bindings_are_redacted():
    source = '''\
creds = {API_KEY: "hunter2"}
other = {settings.API_TOKEN: "hunter2"}
match payload:
    case {"token": "abc"}:
        pass
'''

    safe = _safe(source)

    assert "hunter2" not in safe
    assert "abc" not in safe
    assert "{API_KEY:" in safe
    assert "settings.API_TOKEN" in safe
    assert len(safe.splitlines()) == len(source.splitlines())


def test_annotations_and_call_expressions_stay_faithful():
    source = '''\
api_key: str
def configure(token: str | None = None):
    return token
token = get_token(user)
password: str = Field(min_length=8)
os.getenv("GITHUB_TOKEN")
'''

    safe = _safe(source)

    assert "api_key: str\n" in safe
    assert "def configure(token: str | None = None):" in safe
    assert "token = get_token(user)\n" in safe
    assert "password: str = Field(min_length=" in safe
    assert 'os.getenv("GITHUB_TOKEN")' in safe


def test_a_secret_shaped_name_token_inside_a_span_is_redacted():
    name = "ghp_" + "a" * 20

    safe = _safe(f"token = {name}\n")

    assert name not in safe
    assert safe == f"token = {REDACTION_MARKER}\n"


def test_an_fstring_in_a_credential_position_is_redacted():
    safe = _safe('password = f"hunter2"\n')

    assert "hunter2" not in safe
    assert safe.startswith("password = ")


def test_other_literals_and_comments_use_the_text_rules_line_faithfully():
    source = '''\
# api_key=comment-value
note = "api_key=literal-value"
pem = """-----BEGIN PRIVATE KEY-----
private-material
-----END PRIVATE KEY-----"""
'''

    safe = _safe(source)

    assert "comment-value" not in safe
    assert "literal-value" not in safe
    assert "private-material" not in safe
    assert len(safe.splitlines()) == len(source.splitlines())


def test_handles_non_ascii_before_a_credential_literal_and_multiline_values():
    source = 'label = "é"\nAPI_KEY = "first\\nsecond"\n'

    safe = _safe(source)

    assert 'label = "é"' in safe
    assert "first" not in safe
    assert len(safe.splitlines()) == len(source.splitlines())


def test_non_python_source_returns_none():
    assert sanitize_python_source("def broken(:\n") is None


def test_position_failures_return_none_and_log_the_fallback(monkeypatch, caplog):
    # tokenize splits rows on a bare CR too, so it is redacted in place.
    bare_cr = sanitize_python_source('x = 1\rAPI_KEY = "value"\r')
    assert bare_cr == f'x = 1\rAPI_KEY = "{REDACTION_MARKER}"\r'

    original = tokenize.generate_tokens

    def _shifted(reader):
        for token in original(reader):
            yield token._replace(start=(token.start[0] + 50, token.start[1]))

    monkeypatch.setattr(python_source_redaction.tokenize, "generate_tokens", _shifted)
    with caplog.at_level("WARNING", logger=python_source_redaction.__name__):
        assert sanitize_python_source('API_KEY = "value"\n') is None
    assert "fell back to text rules: token position" in caplog.text


def test_an_unknown_token_kind_is_never_a_kept_code_token(monkeypatch):
    # 3.12+ splits f-strings into FSTRING_* parts and 3.14 adds TSTRING_*.
    # Simulate a new kind so the rule is covered on every interpreter.
    original = tokenize.generate_tokens

    def _with_new_kind(reader):
        for token in original(reader):
            if token.type in {tokenize.NAME, tokenize.OP, tokenize.STRING}:
                token = token._replace(type=token.type + 1000)
            yield token

    monkeypatch.setattr(python_source_redaction.tokenize, "generate_tokens", _with_new_kind)
    assert _safe("token = abc\n") == f"token = {REDACTION_MARKER}\n"


def test_token_formats_in_python_literals_are_redacted_and_breaks_preserved():
    source = '''\
if value == "sk-aaaaaaaaaaaaaaaaaaaaaaaa":
    values = ["ghp_aaaaaaaaaaaaaaaaaaaaaaaa", "AKIAAAAAAAAAAAAA"]
text = """first\rsecond sk-aaaaaaaaaaaaaaaaaaaaaaaa \x0cthird\u2028fourth"""
'''

    safe = _safe(source)

    assert "sk-" not in safe
    assert "ghp_" not in safe
    assert "AKIA" not in safe
    assert len(safe.splitlines()) == len(source.splitlines())


@pytest.mark.asyncio
async def test_read_tool_python_boundary_and_fallbacks(tmp_path, monkeypatch):
    from host_access.credential_sanitizer import CredentialSafeText
    from tools import sandbox_config
    from tools.filesystem_rw import ReadFileTool
    from tools.registry import ToolRegistry

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [tmp_path])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    source = tmp_path / "sample.py"
    source.write_text('API_KEY = "read-value"\nplain = "safe"\n', encoding="utf-8")
    broken = tmp_path / "broken.py"
    broken.write_text('api_key = "fallback-value"\ndef broken(:\n', encoding="utf-8")
    text = tmp_path / "sample.txt"
    text.write_text('api_key = "text-value"\n', encoding="utf-8")
    reader = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)

    direct = await reader.execute(str(source))
    partial = await reader.execute(str(source), offset=2, limit=1)
    assert isinstance(direct, CredentialSafeText)
    assert partial == direct.splitlines()[1]
    assert "read-value" not in direct
    registry = ToolRegistry()
    registry.register(reader)

    async def _context(self):
        return {"id": "thread-1", "project_id": None}, None

    async def _reminder(result, _name, _params):
        return f"{result}\nreminder"

    monkeypatch.setattr(ToolRegistry, "_load_thread_context", _context)
    monkeypatch.setattr("tools.registry.remind_current_turn", _reminder)
    rendered = await registry.execute("read_file", {"path": str(source)})
    fallback = await registry.execute("read_file", {"path": str(broken)})
    plain_text = await registry.execute("read_file", {"path": str(text)})
    assert type(rendered) is str
    assert rendered.endswith("reminder")
    assert fallback == f"{sanitize_credential_text(await reader.execute(str(broken)))}\nreminder"
    assert plain_text == f"{sanitize_credential_text(await reader.execute(str(text)))}\nreminder"

def _tokens_until_error(content: str) -> list[tokenize.TokenInfo]:
    # Redacting a private key with no END marker reaches the end of the file,
    # so the output can end inside an open bracket.
    tokens: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(content).readline):
            tokens.append(token)
    except tokenize.TokenError:
        pass
    return tokens


def _kept_mask(content: str) -> bytearray:
    """Per-character mask of code that may stay visible: NAME/OP and string quotes."""
    shape = bytearray(len(content))
    for start, end in credential_spans(content, never_code=True):
        shape[start:end] = b"\x01" * (end - start)
    starts = [0]
    for line in content.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    mask = bytearray(len(content))
    for token in _tokens_until_error(content):
        if token.type not in {tokenize.NAME, tokenize.OP, tokenize.STRING}:
            continue
        start = starts[token.start[0] - 1] + token.start[1]
        end = starts[token.end[0] - 1] + token.end[1]
        if token.type == tokenize.STRING:
            text = token.string
            head = len(text) - len(text.lstrip("rRbBuUfF"))
            quote = 3 if text[head:head + 3] in {'"""', "'''"} else 1
            mask[start:start + head + quote] = b"\x01" * (head + quote)
            mask[end - quote:end] = b"\x01" * quote
        elif not any(shape[start:end]):
            mask[start:end] = b"\x01" * (end - start)
    return mask


def _marker_run(text: str) -> str:
    return REDACTION_MARKER + "".join(python_source_redaction._BREAK_RE.findall(text))


def _assert_falsification_property(content: str, safe: str) -> None:
    # (a) line count unchanged.
    assert len(safe.splitlines()) == len(content.splitlines())
    # (b) every character inside a span is redacted unless it is a kept code
    # character; every character outside every span is unchanged.
    inside = bytearray(len(content))
    for start, end in credential_spans(content):
        inside[start:end] = b"\x01" * (end - start)
    ones = int.from_bytes(b"\x01" * len(content), "big")
    redacted = (
        int.from_bytes(inside, "big") & ~int.from_bytes(_kept_mask(content), "big") & ones
    ).to_bytes(len(content), "big")
    expected: list[str] = []
    cursor = 0
    for run in re.finditer(b"\x01+", redacted):
        expected.append(content[cursor:run.start()])
        expected.append(_marker_run(content[run.start():run.end()]))
        cursor = run.end()
    expected.append(content[cursor:])
    assert safe == "".join(expected)
    # (c) re-scanning the output finds nothing outside kept code or a marker.
    allowed = _kept_mask(safe)
    for marker in re.finditer(re.escape(REDACTION_MARKER), safe):
        end = marker.end()
        while python_source_redaction._BREAK_RE.match(safe, end):
            end = python_source_redaction._BREAK_RE.match(safe, end).end()
        allowed[marker.start():end] = b"\x01" * (end - marker.start())
    for start, end in credential_spans(safe):
        assert allowed[start:end].count(0) == 0, safe[max(0, start - 20):end + 20]
    # (d) independent of the keep rule: no credential the text rules recognize
    # by its own shape survives, whatever its characters tokenize as. Counted,
    # because the same text may also appear outside a credential.
    for start, end in credential_spans(content, shape_only=True):
        secret = content[start:end]
        assert safe.count(secret) < content.count(secret), secret[:8]


_PEM_BEGIN = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
_PEM_END = "-" * 5 + "END PRIVATE KEY" + "-" * 5

# Built by concatenation so the source itself holds no token-shaped text.
_VENDOR = (
    "ya29" + "." + "a0AfH6" + "b" * 16,
    "glpat" + "-" + "AbcdefGhijklmnopQrst",
    "sk" + "-ant-api03-" + "AbCdefghijklmnopqrstu",
)
_PEM_BODY = ("body-line-one", "body-line-two")
_ADVERSARIAL = [
    # Consecutive comment lines.
    (f"# {_PEM_BEGIN}\n# body-line-one\n# body-line-two\n# {_PEM_END}\nx = 1\n", _PEM_BODY),
    # Implicitly concatenated literals under a non-credential name.
    (
        f'SAMPLE_PEM = (\n    "{_PEM_BEGIN}\\n"\n    "body-line-one\\n"\n'
        f'    "body-line-two\\n"\n    "{_PEM_END}"\n)\n',
        _PEM_BODY,
    ),
    # Items of a joined list.
    (
        f'SAMPLE_PEM = "\\n".join([\n    "{_PEM_BEGIN}",\n    "body-line-one",\n'
        f'    "body-line-two",\n    "{_PEM_END}",\n])\n',
        _PEM_BODY,
    ),
    # No END marker: the key runs to the end of the file.
    (
        f'SAMPLE_PEM = (\n    "{_PEM_BEGIN}\\n"\n    "body-line-one\\n"\n'
        f'    "body-line-two\\n"\n)\n',
        _PEM_BODY,
    ),
    # Call keywords and Field defaults.
    ('client = Client(api_key="hunter2", region="us")\n', ("hunter2",)),
    ('token = issue_token(user, kind="agent")\n', ()),
    ('password: str = Field(default="hunter2", min_length=8)\n', ()),
    # Dict and match keys that are not string literals.
    ('creds = {API_KEY: "hunter2"}\nother = {settings.API_TOKEN: "hunter2"}\n', ("hunter2",)),
    ('match payload:\n    case {"token": "abc"}:\n        pass\n', ("abc",)),
    # f-strings in a credential position and a comparison.
    ('password = f"{prefix}-hunter2-{suffix}"\nif token == "hunter2":\n    pass\n', ()),
    # Secret-shaped bare names and multi-line values.
    (f"token = ghp_{'a' * 20}\nAPI_KEY = \"\"\"line one\nline two\"\"\"\n", ("ghp_",)),
    # Vendor tokens that tokenize as an attribute access or a subtraction chain.
    (f"GOOGLE_TOKEN = {_VENDOR[0]}\nclient.call({_VENDOR[0]})\n", ("a0AfH6",)),
    (f"GITLAB_TOKEN = {_VENDOR[1]}\n", ("AbcdefGhij",)),
    (f"ANTHROPIC_KEY = {_VENDOR[2]}\nvalue = {_VENDOR[2]}\n", ("api03", "AbCdef")),
    # URL userinfo and auth headers.
    ('url = "https://user:hunter2@example.test/x"\n', ("hunter2",)),
]


@pytest.mark.parametrize(("source", "secrets"), _ADVERSARIAL)
def test_adversarial_sources_meet_the_falsification_property(source, secrets):
    safe = _safe(source)

    for secret in secrets:
        assert secret not in safe
    _assert_falsification_property(source, safe)


def test_only_read_and_grep_construct_the_safe_text_marker():
    root = Path(__file__).parents[1]
    constructors = sorted(
        path for path in root.rglob("*.py")
        if "tests" not in path.parts
        and path.name != "credential_sanitizer.py"
        and "CredentialSafeText(" in path.read_text(encoding="utf-8")
    )
    assert constructors == [root / "tools" / "filesystem_rw.py", root / "tools" / "grep_content.py"]


# The corpus grows with the backend, so one sweep would eventually outrun the
# per-test timeout. A stable hash of each relative path splits it into shards
# that together still cover every file exactly once.
_CORPUS_SHARDS = 16


@pytest.mark.parametrize("shard", range(_CORPUS_SHARDS))
def test_corpus_meets_the_falsification_property(shard):
    # A None is only acceptable for a file this module cannot prove is Python.
    root = Path(__file__).parents[1]
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if zlib.crc32(relative) % _CORPUS_SHARDS != shard:
            continue
        content = path.read_text(encoding="utf-8")
        safe = sanitize_python_source(content)
        assert safe is not None, path
        if safe != content:
            _assert_falsification_property(content, safe)
