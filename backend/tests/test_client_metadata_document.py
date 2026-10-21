"""The published client metadata document is well formed and the setting that points at it is off by default."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
DOCUMENT = Path(__file__).resolve().parents[2] / "docs" / "client-metadata" / "corsia-client.json"


def test_the_document_describes_a_secretless_native_loopback_client():  # C-R2
    data = json.loads(DOCUMENT.read_text())
    assert data["client_name"] == "CORSIA" and data["application_type"] == "native"
    assert data["token_endpoint_auth_method"] == "none" and "client_secret" not in data
    assert set(data["grant_types"]) == {"authorization_code", "refresh_token"} and data["response_types"] == ["code"]
    hosts = {urlsplit(uri).hostname for uri in data["redirect_uris"]}
    assert hosts == {"127.0.0.1", "localhost", "::1"}
    assert all(urlsplit(uri).scheme == "http" and urlsplit(uri).path == "/callback" for uri in data["redirect_uris"])


def test_the_client_id_is_the_https_address_of_the_file_itself():  # spec: client_id is the document URL
    parts = urlsplit(json.loads(DOCUMENT.read_text())["client_id"])
    assert parts.scheme == "https" and parts.path == f"/{DOCUMENT.name}" and not parts.query and not parts.fragment


def _setting(env_value):
    """The setting as a fresh process reads it, so no test can leave a reloaded config behind."""
    env = {**os.environ}
    env.pop("CORSIA_CLIENT_METADATA_URL", None)
    if env_value is not None:
        env["CORSIA_CLIENT_METADATA_URL"] = env_value
    code = "import infra.config as c; print(repr(c.CLIENT_METADATA_URL))"
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_the_metadata_setting_is_empty_by_default():  # C-R2
    assert _setting(None) == "''"
    assert _setting("https://example.test/corsia-client.json") == "'https://example.test/corsia-client.json'"
