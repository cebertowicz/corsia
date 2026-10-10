from __future__ import annotations

import hashlib
import json

import pytest


def _seed_tool_result(db, *, thread_id: str, message_id: str, content: str) -> str:
    raw = content.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        (thread_id, "user-1", "native/test", "t", "t"),
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            message_id,
            thread_id,
            "tool",
            content,
            json.dumps({
                "tool_call_id": "call-1",
                "name": "read_file",
                "tool_result_sha256": digest,
                "tool_result_byte_count": len(raw),
            }),
            "t",
            1,
        ),
    )
    db.commit()
    return digest


def test_scoped_page_reads_exact_bounded_utf8_bytes(isolated_db) -> None:
    from core.tool_result_projection import resolve_tool_result_page
    from infra.database import get_db

    db = get_db()
    content = "abc🙂defgh"
    digest = _seed_tool_result(
        db,
        thread_id="thread-1",
        message_id="result-1",
        content=content,
    )

    first = resolve_tool_result_page(
        thread_id="thread-1",
        message_id="result-1",
        sha256=digest,
        offset_bytes=0,
        limit_bytes=7,
        db=db,
    )
    second = resolve_tool_result_page(
        thread_id="thread-1",
        message_id="result-1",
        sha256=digest,
        offset_bytes=first["next_offset_bytes"],
        limit_bytes=7,
        db=db,
    )

    assert first == {
        "version": 1,
        "kind": "corsia_tool_result_page",
        "thread_id": "thread-1",
        "message_id": "result-1",
        "sha256": digest,
        "byte_count": len(content.encode("utf-8")),
        "offset_bytes": 0,
        "next_offset_bytes": 7,
        "content": "abc🙂",
        "complete": False,
    }
    assert second["content"] == "defgh"
    assert second["complete"] is True
    assert second["next_offset_bytes"] == len(content.encode("utf-8"))
    db.close()


@pytest.mark.parametrize(
    ("thread_id", "digest", "match"),
    [
        ("thread-2", None, "another thread|missing"),
        ("thread-1", "0" * 64, "digest"),
    ],
)
def test_scoped_page_rejects_cross_thread_and_digest(
    isolated_db,
    thread_id,
    digest,
    match,
) -> None:
    from core.tool_result_projection import (
        ToolResultReferenceError,
        resolve_tool_result_page,
    )
    from infra.database import get_db

    db = get_db()
    actual = _seed_tool_result(
        db,
        thread_id="thread-1",
        message_id="result-1",
        content="canonical bytes",
    )
    with pytest.raises(ToolResultReferenceError, match=match):
        resolve_tool_result_page(
            thread_id=thread_id,
            message_id="result-1",
            sha256=digest or actual,
            offset_bytes=0,
            limit_bytes=8,
            db=db,
        )
    db.close()


@pytest.mark.asyncio
async def test_registry_retrieval_tool_uses_backend_thread_identity_and_caps_page(
    isolated_db,
) -> None:
    from infra.database import get_db
    from tools.tool_results import ReadToolResultTool

    db = get_db()
    digest = _seed_tool_result(
        db,
        thread_id="thread-1",
        message_id="result-1",
        content="0123456789",
    )
    db.close()

    tool = ReadToolResultTool()
    payload = json.loads(await tool.execute(
        message_id="result-1",
        sha256=digest,
        offset_bytes=2,
        limit_bytes=4,
        _thread_id="thread-1",
    ))
    assert payload["content"] == "2345"
    assert payload["next_offset_bytes"] == 6
    assert payload["complete"] is False

    denied = await tool.execute(
        message_id="result-1",
        sha256=digest,
        offset_bytes=0,
        limit_bytes=4,
        _thread_id="thread-2",
    )
    assert denied.startswith("Error:")

    schema = tool.to_schema()["function"]["parameters"]
    assert "_thread_id" not in schema.get("properties", {})
    assert "_thread_id" not in schema.get("required", [])


@pytest.mark.parametrize(
    ("offset_bytes", "limit_bytes", "match"),
    [
        (-1, 4, "offset"),
        (0, 0, "limit"),
        (0, -1, "limit"),
        (0, 1_000_000, "limit"),
        (1_000_000, 4, "offset"),
    ],
)
def test_scoped_page_rejects_invalid_or_oversized_bounds(
    isolated_db,
    offset_bytes,
    limit_bytes,
    match,
) -> None:
    from core.tool_result_projection import (
        ToolResultReferenceError,
        resolve_tool_result_page,
    )
    from infra.database import get_db

    db = get_db()
    digest = _seed_tool_result(
        db,
        thread_id="thread-1",
        message_id="result-1",
        content="canonical bytes",
    )
    with pytest.raises(ToolResultReferenceError, match=match):
        resolve_tool_result_page(
            thread_id="thread-1",
            message_id="result-1",
            sha256=digest,
            offset_bytes=offset_bytes,
            limit_bytes=limit_bytes,
            db=db,
        )
    db.close()


def test_scoped_page_rejects_offset_inside_utf8_code_point(isolated_db) -> None:
    from core.tool_result_projection import (
        ToolResultReferenceError,
        resolve_tool_result_page,
    )
    from infra.database import get_db

    db = get_db()
    digest = _seed_tool_result(
        db,
        thread_id="thread-1",
        message_id="result-1",
        content="abc🙂def",
    )
    with pytest.raises(ToolResultReferenceError, match="UTF-8|boundary"):
        resolve_tool_result_page(
            thread_id="thread-1",
            message_id="result-1",
            sha256=digest,
            offset_bytes=4,
            limit_bytes=4,
            db=db,
        )
    db.close()


@pytest.mark.parametrize("row_kind", ["assistant", "destructively_retained"])
def test_scoped_page_rejects_noncanonical_rows(isolated_db, row_kind) -> None:
    from core.thread_lifecycle import TOOL_TRIM_MARKER
    from core.tool_result_projection import (
        ToolResultReferenceError,
        resolve_tool_result_page,
        tool_result_integrity_metadata,
    )
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t','t')"
    )
    role = "assistant" if row_kind == "assistant" else "tool"
    content = (
        "not a tool result"
        if row_kind == "assistant"
        else TOOL_TRIM_MARKER + " unavailable"
    )
    raw = content.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "result-1",
            "thread-1",
            role,
            content,
            json.dumps(tool_result_integrity_metadata(content)),
            "t",
            1,
        ),
    )
    db.commit()

    with pytest.raises(
        ToolResultReferenceError,
        match="missing|tool|destructively retained",
    ):
        resolve_tool_result_page(
            thread_id="thread-1",
            message_id="result-1",
            sha256=digest,
            offset_bytes=0,
            limit_bytes=4,
            db=db,
        )
    db.close()


@pytest.mark.parametrize("metadata_field", ["tool_result_sha256", "tool_result_byte_count"])
def test_scoped_page_rejects_metadata_integrity_mismatch(
    isolated_db,
    metadata_field,
) -> None:
    from core.tool_result_projection import (
        ToolResultReferenceError,
        resolve_tool_result_page,
    )
    from infra.database import get_db

    db = get_db()
    digest = _seed_tool_result(
        db,
        thread_id="thread-1",
        message_id="result-1",
        content="canonical bytes",
    )
    metadata = json.loads(db.execute(
        "SELECT metadata FROM messages WHERE id='result-1'"
    ).fetchone()["metadata"])
    metadata[metadata_field] = (
        "0" * 64 if metadata_field.endswith("sha256") else 999
    )
    db.execute(
        "UPDATE messages SET metadata=? WHERE id='result-1'",
        (json.dumps(metadata),),
    )
    db.commit()

    with pytest.raises(ToolResultReferenceError, match="integrity"):
        resolve_tool_result_page(
            thread_id="thread-1",
            message_id="result-1",
            sha256=digest,
            offset_bytes=0,
            limit_bytes=4,
            db=db,
        )
    db.close()
