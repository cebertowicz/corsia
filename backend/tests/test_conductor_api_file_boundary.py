"""The API path cannot bypass the scoped filesystem's hard floor."""
from pathlib import Path

import pytest

from tools.corsia_api import ApiCallError, _scope
from tools.conductor_api_policy import safe_response


@pytest.mark.parametrize("path", [str(Path.home() / ".ssh" / "id_ed25519"),
                                    str(Path.home() / ".corsia" / "state.db"),
                                    "/tmp/.env", "/tmp/private.key"])
def test_private_file_input_denied(path):
    with pytest.raises(ApiCallError, match="Protected file paths"):
        _scope("GET", "/api/files/content", {"path": path}, None)


def test_token_counts_are_preserved_and_credentials_removed():
    assert safe_response({"input_tokens": 42, "max_tokens": 80, "access_token": "sentinel"}) == {
        "input_tokens": 42, "max_tokens": 80,
    }
