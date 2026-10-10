"""Shared threads-row loader contract for live steering adapters."""

from __future__ import annotations

import pytest

from infra.queries import load_thread_row


class _Cursor:
    def __init__(self, row: dict | None, error: BaseException | None = None) -> None:
        self._row = row
        self._error = error

    def fetchone(self) -> dict | None:
        if self._error is not None:
            raise self._error
        return self._row


class _Connection:
    def __init__(
        self,
        row: dict | None,
        execute_error: BaseException | None = None,
        fetch_error: BaseException | None = None,
    ) -> None:
        self._row = row
        self._execute_error = execute_error
        self._fetch_error = fetch_error
        self.closed = False

    def execute(self, _query: str, _params: tuple[str]) -> _Cursor:
        if self._execute_error is not None:
            raise self._execute_error
        return _Cursor(self._row, self._fetch_error)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("row", "execute_error", "fetch_error", "expected"),
    [
        (
            {"id": "known", "user_id": "user-1"},
            None,
            None,
            {"id": "known", "user_id": "user-1"},
        ),
        (None, None, None, {}),
        (None, RuntimeError("execute failed"), None, None),
        (None, None, RuntimeError("fetch failed"), None),
    ],
)
def test_load_thread_row_preserves_output_and_closes_connection(
    monkeypatch, row, execute_error, fetch_error, expected,
) -> None:
    connection = _Connection(row, execute_error, fetch_error)
    monkeypatch.setattr("infra.queries.get_db", lambda: connection)

    error = execute_error or fetch_error
    if error is not None:
        with pytest.raises(RuntimeError, match=str(error)):
            load_thread_row("known")
    else:
        assert load_thread_row("known") == expected
    assert connection.closed is True
