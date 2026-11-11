"""Artifact links presented on operator plan rows."""


def present(link: dict) -> dict:
    return {
        "id": link["artifact_id"],
        "role": link["role"],
        "integration": link["integration"],
        "type": link["type"],
        "external_id": link["external_id"],
        "title": link.get("title"),
        "path": link.get("path"),
        "state": link["state"],
        "attention_state": link.get("attention_state"),
    }
