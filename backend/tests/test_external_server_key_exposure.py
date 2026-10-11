"""External server rows must never carry a live credential to the client.

``get_servers``/``get_server`` are ``SELECT *`` over ``network_servers``, whose
``metadata_json`` holds the External provider API key. These tests bind the
redaction in :func:`api.network_servers._public_server_view` so a future
"just forward the row" change fails loudly instead of leaking a key.
"""

from __future__ import annotations

import json

from api.network_servers import _public_server_view


def _row(meta: dict) -> dict:
    return {
        "id": "ns-test",
        "provider": "external",
        "kind": "external",
        "dialect": "openai",
        "instance_name": "Alibaba MaaS",
        "metadata_json": json.dumps(meta),
    }


def test_api_key_never_leaves_the_row():
    row = _row({
        "api_key": "sk-ws-ABCDEF123456",
        "base_url": "https://ws-x.us-east-1.maas.aliyuncs.com/compatible-mode/v1",
        "cache_warm_seconds": 300,
    })
    out = _public_server_view(row)

    assert "metadata_json" not in out
    assert "api_key" not in json.dumps(out)
    assert "sk-ws-ABCDEF123456" not in json.dumps(out)


def test_presence_and_mask_survive_redaction():
    out = _public_server_view(_row({}), {"ns-test": "sk-ws-ABCDEF123456"})
    assert out["has_key"] is True
    assert out["masked_key"].endswith("3456")
    assert out["masked_key"].startswith("\u2022\u2022\u2022\u2022")


def test_non_secret_metadata_is_preserved_for_the_ui():
    out = _public_server_view(_row({
        "api_key": "k12345678",
        "base_url": "https://host.example/compatible-mode/v1",
        "cache_warm_seconds": 3600,
        "context_window": 131072,
    }))
    assert out["metadata"]["base_url"] == "https://host.example/compatible-mode/v1"
    assert out["metadata"]["cache_warm_seconds"] == 3600
    assert out["metadata"]["context_window"] == 131072


def test_malformed_or_absent_metadata_degrades_without_raising():
    assert _public_server_view(_row({}))["has_key"] is False
    assert _public_server_view(_row({}))["masked_key"] == ""

    broken = _row({})
    broken["metadata_json"] = "{not json"
    out = _public_server_view(broken)
    assert out["metadata"] == {}
    assert out["has_key"] is False
