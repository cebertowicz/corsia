"""Each lesson-report call leaves one log line naming its caller, arguments, outcome and stage times."""
from __future__ import annotations

import asyncio
import json

from loguru import logger
import pytest

from core import lesson_report_access
from tests.test_lesson_report_adapters import report_target  # noqa: F401

_PREFIX = "lesson_report_call "


def _call_payloads(records):
    lines = [str(record["message"]) for record in records if str(record["message"]).startswith(_PREFIX)]
    return [json.loads(line[len(_PREFIX):]) for line in lines]


async def _logged(tool, **kwargs):
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="INFO")
    try:
        result = json.loads(await tool.execute(_thread_id="owner", **kwargs))
    finally:
        logger.remove(sink)
    return result, _call_payloads(records)


@pytest.mark.asyncio
async def test_a_tool_call_logs_its_caller_arguments_and_where_the_time_went(report_target):  # noqa: F811
    _db, _app, tool = report_target
    result, [payload] = await _logged(tool, scope="all", state="trial", preview=True)

    assert "error" not in result
    assert payload["caller"] == "owner" and payload["outcome"] == "ok" and payload["cursor"] is False
    assert payload["arguments"] == {"scope": "all", "state": "trial", "preview": True}
    assert isinstance(payload["threads"], int) and isinstance(payload["body_read_calls"], int)
    for name in ("collect", "history", "boundaries", "offer_evidence", "read_bodies.candidates",
                 "sources", "correction_join", "render", "project_ids"):
        assert payload["stages"][name]["wall_ms"] >= 0, name


@pytest.mark.asyncio
async def test_a_refused_call_still_logs_one_line_with_its_outcome(report_target):  # noqa: F811
    _db, _app, tool = report_target
    result, [payload] = await _logged(tool, scope="project")

    assert "error" in result
    assert payload["caller"] == "owner" and payload["arguments"] == {"scope": "project"}
    assert payload["outcome"] == "refused" and "threads" not in payload


@pytest.mark.asyncio
async def test_a_caller_supplied_value_cannot_forge_fields_or_lines(report_target):  # noqa: F811
    _db, _app, tool = report_target
    forged = "x outcome=ok total_ms=0\nlesson_report_call {\"outcome\":\"ok\"}"
    _result, payloads = await _logged(tool, scope="all", lesson_id=forged)

    [payload] = payloads
    assert payload["arguments"]["lesson_id"] == forged
    assert set(payload) >= {"caller", "outcome", "total_ms", "stages"}


@pytest.mark.asyncio
async def test_a_cancelled_awaiting_task_logs_cancelled_not_error(report_target, monkeypatch):  # noqa: F811
    _db, _app, tool = report_target

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(lesson_report_access, "run_bulk", cancelled)
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="INFO")
    try:
        with pytest.raises(asyncio.CancelledError):
            await tool.execute(_thread_id="owner", scope="all")
    finally:
        logger.remove(sink)

    [payload] = _call_payloads(records)
    assert payload["outcome"] == "cancelled"
