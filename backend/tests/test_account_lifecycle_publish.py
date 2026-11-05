"""An account leaving signed-in tells open pages their model list changed; a sign-in leaves that to its own refresh and bind."""
import pytest

from connections.account_lifecycle import change_state
from connections.vendor_accounts import list_accounts
from vendor_account_fixtures import accounts  # noqa: F401


def _signed_in():
    return next(row for row in list_accounts(provider="codex") if row["state"] == "signed_in")


@pytest.fixture
def published(monkeypatch):
    calls = []
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", lambda *_a, **_k: calls.append("models_changed"))
    return calls


@pytest.mark.parametrize("state", ["signed_out", "removed"])
def test_an_account_leaving_signed_in_publishes_models_changed(accounts, published, state):
    account = _signed_in()

    change_state(account["id"], state, expected_revision=account["revision"])

    assert published == ["models_changed"]


def test_a_sign_in_publishes_nothing_itself(accounts, published):
    account = _signed_in()
    out = change_state(account["id"], "signed_out", expected_revision=account["revision"])
    published.clear()

    change_state(out["id"], "signed_in", expected_revision=out["revision"])

    assert published == []
