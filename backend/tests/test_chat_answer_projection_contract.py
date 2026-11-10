"""Answer projections have one grammar owner and bounded stream scanning."""

import ast
from pathlib import Path

from infra import chat_comment_answers as answers


def test_private_answer_grammar_is_not_imported_elsewhere():
    backend = Path(__file__).resolve().parents[1]
    private = {'_TAGGED', '_TAG', '_PARTIAL_TAG'}
    violations = []
    for path in backend.rglob('*.py'):
        if path == Path(answers.__file__).resolve():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == 'infra.chat_comment_answers':
                if private.intersection(alias.name for alias in node.names):
                    violations.append(str(path.relative_to(backend)))
    assert not violations


def test_stream_scans_linearly_for_one_character_deltas(monkeypatch):
    scanned = [0]
    class CountingPattern:
        def __init__(self, pattern):
            self.pattern = pattern
        def search(self, text, pos=0):
            scanned[0] += len(text) - pos
            return self.pattern.search(text, pos)
    monkeypatch.setattr(answers, '_TAGGED', CountingPattern(answers._TAGGED))
    monkeypatch.setattr(answers, '_PARTIAL_TAG', CountingPattern(answers._PARTIAL_TAG))
    stream = answers.VisibleAnswerStream()
    text = 'x' * 50_000
    assert ''.join(stream.feed(char) for char in text) == text
    assert scanned[0] <= len(text) * 8
    before = scanned[0]
    for char in '\n\n[c1.1] secret' + 'z' * 1000:
        stream.feed(char)
    after = scanned[0]
    stream.feed('z' * 1000)
    assert scanned[0] == after
    assert after - before < 200


def test_stream_preserves_partial_boundary_across_newline_chunks():
    stream = answers.VisibleAnswerStream()
    chunks = ('\n\r', '\n', ' \r', '\n[c1\n', '0')
    assert [stream.feed(chunk) for chunk in chunks] == ['', '', '\n', '', '\r\n \r\n[c1\n0']


def test_stream_scans_long_partial_prefixes_linearly(monkeypatch):
    scanned = [0]

    class CountingPattern:
        def __init__(self, pattern):
            self.pattern = pattern

        def search(self, text, pos=0):
            scanned[0] += len(text) - pos
            return self.pattern.search(text, pos)

    monkeypatch.setattr(answers, '_TAGGED', CountingPattern(answers._TAGGED))
    monkeypatch.setattr(answers, '_PARTIAL_TAG', CountingPattern(answers._PARTIAL_TAG))
    for text in ('\n' + ' ' * 50_000 + 'x', '[c' + '1' * 50_000 + 'x',
                 '\n\n[c1.' + '2' * 50_000 + '] hidden'):
        before = scanned[0]
        stream = answers.VisibleAnswerStream()
        result = ''.join(stream.feed(char) for char in text)
        assert result == ('' if text.endswith('] hidden') else text)
        assert scanned[0] - before <= len(text) * 8
