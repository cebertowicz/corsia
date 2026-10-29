"""A leading JSON scalar must not hide the shell's PR URL."""
import pytest

from infra.database import get_db
from tests.registry_helpers import make_session
from tests.test_shell_pr_registration import _direct_shell_context, _pr_rows, _stub_process, _URL
from tools.shell import BashTool


@pytest.mark.asyncio
async def test_scalar_line_before_pr_url_registers(isolated_db, monkeypatch):
    db = get_db()
    thread_id = make_session(db, prefix="scalar-pr")
    _stub_process(monkeypatch, stdout="123\n" + _URL + "\n")
    await BashTool().execute("gh pr create --title 'Fix' --body ''", **_direct_shell_context(thread_id))
    assert [row["external_id"] for row in _pr_rows(db, thread_id)] == ["owner/repo#42"]
