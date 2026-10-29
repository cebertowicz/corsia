"""Fetch model IDs from an external provider's model-list endpoint."""

import httpx


async def fetch_model_ids(url: str, dialect: str, headers: dict[str, str]) -> list[str]:
    ids: list[str] = []
    cursor = None
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            kwargs: dict = {"params": {"after_id": cursor}} if cursor else {}
            resp = await client.get(url, headers=headers, **kwargs)
            resp.raise_for_status()
            data = resp.json() or {}
            for model in data.get("data") or []:
                model_id = model.get("id") if isinstance(model, dict) else None
                if isinstance(model_id, str) and model_id:
                    ids.append(model_id)
            if dialect != "anthropic" or not data.get("has_more"):
                return ids
            next_cursor = data.get("last_id")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise ValueError("Anthropic models pagination returned an invalid cursor")
            cursor = next_cursor
