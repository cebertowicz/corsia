"""Publish endpoints confirmed by the oMLX supervisor."""
import logging

log = logging.getLogger("llm.providers.omlx.supervisor")


def mark_endpoint_alive(server_id: str, host: str, port: int, managed: bool = False) -> None:
    """Refresh a confirmed endpoint, invalidating old observations after managed replacement."""
    try:
        from llm.network_endpoints import upsert_endpoint
        from llm.network_discovery import update_server_status
        from llm.server_observation import advance_observation_generation

        if managed:
            advance_observation_generation(server_id)
        upsert_endpoint(server_id, host, ok=True, latency_ms=0, supersede=managed)
        update_server_status(server_id, status="online", latency_ms=0)
    except Exception as exc:
        log.warning("_mark_endpoint_alive failed for %s: %s", server_id, exc)
