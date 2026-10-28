"""Account-bound tools are counted, not named, in the always-on tools_load index."""

import pytest

from tools.catalog import TOOL_CATALOG, pack_members
from tools.registry import ToolRegistry
from tools.tool_surface import pack_index

ACCOUNT_TOOLS = (
    "email_search", "email_read", "email_draft", "email_send", "calendar_list",
    "calendar_draft_invite", "calendar_send_invite", "microsoft_mail_search",
    "microsoft_calendar_list", "microsoft_files_list",
)
SETUP_TOOLS = ("google_connect", "google_status", "microsoft_connect", "microsoft_status", "mail_host_connect")


def render():
    return pack_index(ToolRegistry())


def test_account_tools_are_counted_and_setup_tools_stay_named():
    index = render()
    for name in ACCOUNT_TOOLS:
        assert TOOL_CATALOG[name].account_bound
        assert f"- {name}:" not in index
    for name in SETUP_TOOLS:
        assert not TOOL_CATALOG[name].account_bound
        assert index.count(f"- {name}:") == 1


@pytest.mark.parametrize("pack", ["email", "microsoft_mail", "microsoft_calendar", "microsoft_files"])
def test_each_account_pack_names_its_exact_load_call(pack):
    bound = sum(TOOL_CATALOG[name].account_bound for name in pack_members()[pack])
    assert bound
    index = render()
    assert f"[{pack}] " in index
    assert index.count(f'- {bound} more for a connected account: tools_load(pack="{pack}")') == 1


def test_index_reads_no_account_state(monkeypatch):
    def refuse():
        raise AssertionError("the index must not open the database")

    monkeypatch.setattr("infra.database.get_db", refuse)
    assert render() == render()


def test_pack_load_still_returns_every_account_tool():
    names = {name for pack in ("email", "microsoft_mail") for name in pack_members()[pack]}
    assert {"email_send", "calendar_send_invite", "microsoft_mail_search"} <= names
