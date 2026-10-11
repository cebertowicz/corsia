"""Provider connectivity checks."""

from __future__ import annotations

from infra.db_async import run_in_db
from infra.key_store import resolve_provider


async def run_provider_test(name: str, meta: dict) -> dict:
    api_key = await run_in_db(resolve_provider, name) or ""
    if not api_key:
        return {"ok": False, "error": "No API key configured"}

    test_url = meta.get("test_url")
    if not test_url:
        return {"ok": True, "message": "No test endpoint available - key saved"}

    import httpx
    try:
        if name == "anthropic":
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    test_url, headers=headers,
                    json={"model": "claude-haiku-4-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                )
        else:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(test_url, headers=headers)
        if r.status_code in (200, 400) and name == "anthropic":
            return {"ok": True, "message": "Key is valid"}
        if r.status_code == 200:
            return {"ok": True, "message": "Key is valid"}
        if r.status_code in (401, 403):
            return {"ok": False, "error": "Invalid API key"}
        return {"ok": False, "error": f"Unexpected response: {r.status_code}"}
    except httpx.TimeoutException:
        return {"ok": False, "error": "Connection timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
