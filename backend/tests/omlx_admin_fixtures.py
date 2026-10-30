"""A fake oMLX admin surface that mirrors 0.7.0, checked against a real 0.7.0 server.

``login`` is refused (400) until a key exists; every admin route answers 401
without the ``omlx_admin_session`` cookie unless ``skip_api_key_verification``
was set; ``setup-api-key`` works once (200, then 400); ``global-settings`` reports
the bind host and accepts the skip flag.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llm.providers import omlx_endpoints as ep

KEY = "adminkey-" + "a1" * 12
OTHER_KEY = "adminkey-" + "b2" * 12
CFG = {"server_id": "srv", "host": "127.0.0.1", "port": 8000, "api_key": KEY}


class FakeOmlx:
    def __init__(self, key: str | None = KEY):
        self.key = key
        self.token = "session-0"
        self.host = "127.0.0.1"  # the bind host global-settings reports
        self.skip = False
        self.requests: list[tuple[str, str, str | None, str]] = []  # method, path, cookie, auth header
        self.bodies: list[tuple[str, str, dict]] = []  # method, path, json body
        self.logins = 0
        self.expire_next = 0  # answer this many authenticated calls with 401
        self.setup_status: int | None = None  # force setup-api-key to answer this
        self.settings_status = 200  # what a global-settings POST answers
        # Model ids a settings PUT answers 404 "Model not found" for, as 0.7.0's routes.py does at 2745.
        self.missing_models: set[str] = set()
        self.no_model_route = False  # an oMLX without the model settings route: FastAPI's 404 "Not Found"

    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _cookie, _auth in self.requests]

    def writes(self, path_end: str) -> list[dict]:
        return [body for method, path, body in self.bodies if method == "POST" and path.endswith(path_end)]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        cookie = request.headers.get("cookie")
        self.requests.append((request.method, path, cookie, request.headers.get("authorization", "")))
        body = json.loads(request.content) if request.content else {}
        self.bodies.append((request.method, path, body))
        if path == "/admin/api/login":
            self.logins += 1
            await asyncio.sleep(0.01)  # a real round trip, so concurrent callers overlap
            if self.key is None:
                return httpx.Response(400, json={"detail": "no key"})
            if body.get("api_key") != self.key or body.get("remember") is not False:
                return httpx.Response(401)
            self.token = f"session-{self.logins}"
            return httpx.Response(200, headers={"set-cookie": f"omlx_admin_session={self.token}; Path=/"})
        if path == "/admin/api/setup-api-key":
            if self.setup_status:
                return httpx.Response(self.setup_status)
            if self.key is not None:
                return httpx.Response(400)
            self.key = body["api_key"]
            return httpx.Response(200, headers={"set-cookie": "omlx_admin_session=setup-cookie; Path=/"})
        if not self.skip:
            if self.expire_next:
                self.expire_next -= 1
                return httpx.Response(401)
            if cookie != f"omlx_admin_session={self.token}":
                return httpx.Response(401)
        if path == "/admin/api/global-settings":
            if request.method == "GET":
                return httpx.Response(200, json={"server": {"host": self.host}, "scheduler": {}})
            if self.settings_status != 200:
                return httpx.Response(self.settings_status)
            applied = [name for name in body if name in {
                "max_concurrent_requests", "prefill_priority", "skip_api_key_verification",
            }]
            self.skip = self.skip or body.get("skip_api_key_verification") is True
            return httpx.Response(200, json={"runtime_applied": applied})
        if request.method == "PUT" and path.startswith("/admin/api/models/") and path.endswith("/settings"):
            model_id = path.split("/")[4]
            if self.no_model_route:
                return httpx.Response(404, json={"detail": "Not Found"})
            if model_id in self.missing_models:
                return httpx.Response(404, json={"detail": f"Model not found: {model_id}"})
            return httpx.Response(200, json={"success": True, "model_id": model_id, "settings": {}})
        if path == "/admin/api/hf/download":
            return httpx.Response(202, json={"success": True, "task": {"task_id": "task-1"}})
        if path == "/admin/api/hf/tasks":
            return httpx.Response(200, json={"tasks": [{"task_id": "task-1", "status": "completed"}]})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def omlx(monkeypatch):
    fake = FakeOmlx()
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    ep._sessions.clear()
    ep._login_locks.clear()
    yield fake
    ep._sessions.clear()
    ep._login_locks.clear()
