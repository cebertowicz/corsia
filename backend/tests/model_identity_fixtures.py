"""Catalog and manifest rows for model-identity tests (Models, Spend)."""

from __future__ import annotations

import json

LOCAL_ID = "vmlx-abc12345"
LOCAL_MODEL = "omlx@srv/Fortissimo-8bit"


def seed_identity_catalog(db) -> None:
    """Add a local oMLX row and a listed concrete Claude Code row to the test catalog.

    The shared test catalog already holds the ``claude-code/opus`` alias row,
    which resolves to ``claude-opus-4-8`` today. Its name carries that version,
    as the real catalog's does.
    """
    db.execute("UPDATE models SET display_name='Opus 4.8' WHERE id='claude-code/opus'")
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,"
        "enabled,available,metadata_json,created_at,updated_at) VALUES "
        "(?,'native','omlx','Fortissimo-8bit',?,'Fortissimo','network','discovered',1,1,'{}','t','t'),"
        "('claude-code/claude-opus-4-8','claude-code','claude-code','claude-opus-4-8','claude-code/claude-opus-4-8',"
        "'Opus listed','harness','discovered',1,1,?,'t','t')",
        (LOCAL_ID, LOCAL_MODEL, json.dumps({"resolved_identity": "claude-opus-4-8"})),
    )


def seed_manifest(db, manifest_id: str, harness: str, resolved: str | None, source: str | None) -> None:
    db.execute(
        "INSERT INTO context_manifests (id,harness,resolved_model,resolved_model_source,entries_json,entry_count,created_at) "
        "VALUES (?,?,?,?,'[]',0,'t')",
        (manifest_id, harness, resolved, source),
    )


ALIBABA_HOST = "token-plan.ap-southeast-1.maas.aliyuncs.com"


def seed_external_server(db, server_id: str, host: str, endpoint_kind: str = "public") -> None:
    """Add an External server reached at ``host`` (``public`` makes it a cloud server)."""
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,dialect,instance_id,instance_name,port,status,"
        "last_seen_at,created_at,updated_at) VALUES (?,'external','external','openai',?,?,443,'online','t','t','t')",
        (server_id, server_id, server_id),
    )
    db.execute(
        "INSERT INTO network_server_endpoints (id,server_id,host,kind,created_at,updated_at) VALUES (?,?,?,?,'t','t')",
        (f"ep-{server_id}", server_id, host, endpoint_kind),
    )
