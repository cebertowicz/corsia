from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row
        self.calls = []
        self.closed = False

    def execute(self, query, params):
        self.calls.append((query, params))
        return _Cursor(self._row)

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_default_renderer_reads_thread_row_and_preserves_conflicts(monkeypatch):
    import core.destination_preflight as destination_preflight
    import infra.database as database
    import infra.db_async as db_async
    import infra.preflight_render as preflight_render
    from infra.input_render import InputProviderProjection
    from thread import model_projection
    from thread.model_state import ModelSwitchConflict

    matching_row = {"id": "thread-1", "model": "native/source"}
    rows = [matching_row, None, {"id": "thread-1", "model": "native/other"}]
    connections = []
    callback_names = []

    def get_db():
        connection = _Connection(rows.pop(0))
        connections.append(connection)
        return connection

    async def immediate(callback):
        callback_names.append(callback.__name__)
        return callback()

    rendered = object()
    render = AsyncMock(return_value=rendered)
    monkeypatch.setattr(database, "get_db", get_db)
    monkeypatch.setattr(db_async, "run_in_db", immediate)
    monkeypatch.setattr(preflight_render, "render_preflight_selection", render)
    fit_input = InputProviderProjection(
        identity="fit-1",
        content="input",
        source_content="source input",
        agent_envelope=object(),
        attachments=(),
        media_projection=None,
        receipts=(),
    )
    arguments = {
        "thread_id": "thread-1",
        "current_model": "native/source",
        "expected_selected_model": "native/source",
        "target": SimpleNamespace(harness="native", catalog_id="native/target"),
        "replay": object(),
        "fit_input": fit_input,
    }

    assert await model_projection._default_model_switch_renderer(**arguments) is rendered
    assert callback_names == ["_read_thread_row"]
    assert render.await_args.kwargs["thread_row"] is matching_row

    for _ in range(2):
        with pytest.raises(
            ModelSwitchConflict,
            match="model switch source changed before destination rendering",
        ):
            await model_projection._default_model_switch_renderer(**arguments)

    assert callback_names == ["_read_thread_row"] * 3
    assert render.await_count == 1
    assert all(
        connection.calls
        == [("SELECT * FROM threads WHERE id = ?", ("thread-1",))]
        and connection.closed
        for connection in connections
    )
