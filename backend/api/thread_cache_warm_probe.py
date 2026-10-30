"""Read an oMLX cache probe for the Native browser-warm endpoint."""

from __future__ import annotations

from llm.resolution import ExecutionTarget


def warm_transport_identity(target: ExecutionTarget) -> tuple[str, str] | None:
    from llm.local_backends import OmlxBackend, find_backend

    backend = find_backend(target.transport_model)
    if not isinstance(backend, OmlxBackend):
        return None
    server_id, model_id = backend._parse(target.transport_model)
    if not server_id or not model_id:
        return None
    return server_id, model_id


def shape_warm_messages(messages: list[dict], target: ExecutionTarget) -> list[dict]:
    from llm.providers.omlx_messages import shape_messages

    identity = warm_transport_identity(target)
    if identity is None:
        return messages
    server_id, model_id = identity
    return shape_messages(messages, f"openai/{model_id}", server_id)


def probe_progress(probe: dict) -> dict:
    return {
        "total_blocks": probe.get("total_blocks"),
        "cold_blocks_at_start": probe.get("blocks_cold", 0),
        "block_size": probe.get("block_size", 0),
        "total_tokens": probe.get("total_tokens", 0),
        "ready_blocks": probe.get("blocks_ssd_hot"),
    }


def probe_is_memory_hot(probe: dict) -> bool:
    total = int(probe.get("total_blocks") or 0)
    block_size = int(probe.get("block_size") or 0)
    total_tokens = int(probe.get("total_tokens") or 0)
    complete_blocks = min(total, total_tokens // block_size) if block_size > 0 else 0
    partial_blocks = total - complete_blocks
    return bool(
        probe.get("ok")
        and complete_blocks > 0
        and int(probe.get("blocks_ssd_hot") or 0) >= complete_blocks
        and int(probe.get("blocks_ssd_disk") or 0) == 0
        and int(probe.get("blocks_cold") or 0) <= partial_blocks
    )
