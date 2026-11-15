"""Each lesson-report call leaves one log line naming its caller, arguments, outcome and phase times."""
from __future__ import annotations

import json
import re

from loguru import logger
import pytest

from tests.test_lesson_report_adapters import report_target  # noqa: F401


def _call_lines(records):
    return [str(record["message"]) for record in records if str(record["message"]).startswith("lesson_report.call ")]


@pytest.mark.asyncio
async def test_a_tool_call_logs_its_caller_arguments_and_where_the_time_went(report_target):  # noqa: F811
    _db, _app, tool = report_target
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="INFO")
    try:
        result = json.loads(await tool.execute(_thread_id="owner", scope="all", state="trial", preview=True))
    finally:
        logger.remove(sink)

    assert "error" not in result
    [line] = _call_lines(records)
    assert "caller=owner " in line
    assert "scope=all " in line and "state=trial " in line and "preview=True " in line
    assert "cursor=no outcome=ok " in line
    assert re.search(r" threads=\d+ body_read_calls=\d+ ", line)
    phases = dict(item.split(":") for item in line.split(" phases=", 1)[1].split(","))
    for name in ("collect", "history", "boundaries", "offer_evidence", "read_bodies.candidates",
                 "sources", "correction_join", "render", "project_ids"):
        assert float(phases[name]) >= 0, name


@pytest.mark.asyncio
async def test_a_refused_call_still_logs_one_line_with_its_outcome(report_target):  # noqa: F811
    _db, _app, tool = report_target
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="INFO")
    try:
        result = json.loads(await tool.execute(_thread_id="owner", scope="project"))
    finally:
        logger.remove(sink)

    assert "error" in result
    [line] = _call_lines(records)
    assert "caller=owner scope=project " in line and "outcome=refused " in line
    assert " threads=" not in line
