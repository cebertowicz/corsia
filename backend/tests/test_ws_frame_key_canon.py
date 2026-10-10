"""Every WebSocket frame names the thread the way the browser reads it.

The frames the backend emits and the fields the frontend reads are one
contract, but nothing enforced it: backend tests assert whatever the backend
emits, and the frontend tests use their own fixtures. A rename can therefore
move one side and leave the other, and both suites stay green while streaming
silently stops -- which is exactly what happened.

A frame is a dict literal carrying a "type" key. This walks the source for
those and pins the identifier they use.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# Names that are legitimately not a CORSIA thread on the wire. Child and
# target identifiers name CORSIA threads, so they must use ``*_thread_id``.
ALLOWED_SESSION_KEYS = {"cc_session_id", "codex_session_id"}

SKIP_DIRS = {"tests", "__pycache__", ".venv"}


def _frame_dicts(tree: ast.AST):
    """Yield every dict literal that declares a "type" key."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        if "type" in keys:
            yield node, keys


def _sources():
    for path in BACKEND.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def test_no_websocket_frame_names_the_thread_session_id():
    """A frame keyed session_id reaches a browser that reads thread_id."""
    offenders = []
    for path in _sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node, keys in _frame_dicts(tree):
            if "session_id" in keys:
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert not offenders, (
        "these frames name the thread 'session_id' while the frontend reads "
        f"'thread_id', so their events land nowhere: {offenders}"
    )


def test_provider_session_keys_stay_qualified():
    """A harness's own session keeps its prefix, so it never reads as a thread."""
    bare = []
    for path in _sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node, keys in _frame_dicts(tree):
            for key in keys:
                if key.endswith("session_id") and key not in ALLOWED_SESSION_KEYS:
                    bare.append(f"{path.relative_to(BACKEND)}:{node.lineno} {key}")
    assert not bare, (
        "a frame names a session id that is neither a known provider id nor a "
        f"thread: {bare}"
    )
