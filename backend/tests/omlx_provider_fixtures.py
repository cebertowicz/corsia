from __future__ import annotations

from typing import Any

import httpx


CFG_PUBLIC = {
    "server_id": "test-server-01",
    "host": "127.0.0.1",
    "port": 8000,
    "instance_name": "Test Mac",
}
CFG_WITH_KEY = {**CFG_PUBLIC, "api_key": "test-admin-key"}


def _resp(status: int, body: Any = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status, request=httpx.Request("GET", "http://x/"))
    return httpx.Response(
        status, json=body, request=httpx.Request("GET", "http://x/"),
    )


def _login_ok() -> httpx.Response:
    """oMLX's admin sign-in: 200 and the session cookie its admin routes require."""
    return httpx.Response(
        200, json={"success": True}, headers={"set-cookie": "omlx_admin_session=fixture; Path=/"},
        request=httpx.Request("POST", "http://x/"),
    )


class _Router:

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]):
        self.routes = {**routes}
        self.routes.setdefault(("POST", "/admin/api/login"), _login_ok())
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method
        body = {}
        if request.content:
            try:
                import json as _json
                body = _json.loads(request.content)
            except Exception:
                body = {"_raw": request.content[:200].decode(errors="replace")}
        self.calls.append((method, url, body))
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                return resp
        return _resp(404)


def _patch_httpx(monkeypatch, router: _Router):
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(router)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
