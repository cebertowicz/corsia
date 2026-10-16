"""A local fake hosted MCP server and OAuth authorization server for the sign-in tests.

It is an ASGI app reached through ``httpx.ASGITransport`` (patched over
``tools.mcp_oauth_protocol.new_client``), so no network is used. The "browser" is
:meth:`FakeAS.consent`, which reads the authorization link CORSIA built, checks its
PKCE and `resource` fields, and returns the redirect a real provider would send.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

BASE = "http://127.0.0.1:7999"
MCP_URL = f"{BASE}/mcp"
ACCESS = "ACCESS-TOKEN-{n}-aaaaaaaa"
REFRESH = "REFRESH-TOKEN-{n}-rrrrrrrr"
DCR_SECRET = "DCR-CLIENT-SECRET-ssssssss"
DCR_REG_TOKEN = "DCR-REGISTRATION-tttttttt"


class FakeAS:
    def __init__(self, *, iss: bool = False, metadata_doc: bool = False, dcr: bool = True, revoke: bool = True) -> None:
        self.iss, self.metadata_doc, self.dcr, self.revoke = iss, metadata_doc, dcr, revoke
        self.codes: dict[str, dict] = {}
        self.issued = 0
        self.registrations: list[dict] = []
        self.token_requests: list[dict] = []
        self.refresh_requests: list[dict] = []
        self.revoked: list[str] = []
        self.refresh_mode = "ok"      # ok | rejected | malformed | down
        self.token_mode = "ok"        # ok | malformed
        self.revoke_mode = "ok"       # ok | fail | hang
        self.redirect_resource_metadata = False
        self.current_access = ""
        self.valid_access: set[str] = set()
        self.mcp_calls = 0
        self.app = Starlette(routes=[
            Route("/.well-known/oauth-protected-resource{rest:path}", self._resource),
            Route("/.well-known/oauth-authorization-server", self._metadata),
            Route("/register", self._register, methods=["POST"]),
            Route("/token", self._token, methods=["POST"]),
            Route("/revoke", self._revoke, methods=["POST"]),
            Route("/userinfo", self._userinfo),
            Route("/mcp", self._mcp, methods=["POST", "GET"]),
        ])

    def client(self, **_options) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), follow_redirects=False)

    async def _resource(self, request: Request):
        if self.redirect_resource_metadata:
            return PlainTextResponse("", 302, headers={"location": "http://evil.example/steal"})
        return JSONResponse({"resource": MCP_URL, "authorization_servers": [BASE], "scopes_supported": ["read", "write"]})

    async def _metadata(self, request: Request):
        data: dict[str, object] = {
            "issuer": BASE, "authorization_endpoint": f"{BASE}/authorize", "token_endpoint": f"{BASE}/token",
            "userinfo_endpoint": f"{BASE}/userinfo", "code_challenge_methods_supported": ["S256"],
        }
        if self.revoke:
            data["revocation_endpoint"] = f"{BASE}/revoke"
        if self.dcr:
            data["registration_endpoint"] = f"{BASE}/register"
        if self.iss:
            data["authorization_response_iss_parameter_supported"] = True
        if self.metadata_doc:
            data["client_id_metadata_document_supported"] = True
        return JSONResponse(data)

    async def _register(self, request: Request):
        body = await request.json()
        self.registrations.append(body)
        return JSONResponse({"client_id": "dcr-client-1", "client_secret": DCR_SECRET, "registration_access_token": DCR_REG_TOKEN}, 201)

    async def _userinfo(self, request: Request):
        return JSONResponse({"email": "person@example.test"})

    async def _mcp(self, request: Request):
        self.mcp_calls += 1
        auth = request.headers.get("authorization", "")
        if auth.removeprefix("Bearer ") in self.valid_access:
            return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
        return PlainTextResponse("", 401, headers={"www-authenticate": f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'})

    async def _revoke(self, request: Request):
        form = parse_qs((await request.body()).decode())
        self.revoked.append(form.get("token", [""])[0])
        if self.revoke_mode == "fail":
            return PlainTextResponse("", 500)
        if self.revoke_mode == "hang":
            raise httpx.ReadTimeout("slow provider")
        return PlainTextResponse("")

    def mint(self) -> tuple[str, str]:
        self.issued += 1
        access, refresh = ACCESS.format(n=self.issued), REFRESH.format(n=self.issued)
        self.valid_access = {access}
        self.current_access = access
        return access, refresh

    async def _token(self, request: Request):
        form = {key: values[0] for key, values in parse_qs((await request.body()).decode()).items()}
        if form.get("grant_type") == "refresh_token":
            self.refresh_requests.append(form)
            if self.refresh_mode == "rejected":
                return JSONResponse({"error": "invalid_grant"}, 400)
            if self.refresh_mode == "malformed":
                return PlainTextResponse('not json {"access_token":"LEAKED-access-token-zzzz"', 200)
            if self.refresh_mode == "down":
                return PlainTextResponse("", 503)
            access, refresh = self.mint()
            return JSONResponse({"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600})
        self.token_requests.append(form)
        record = self.codes.get(form.get("code", ""))
        if self.token_mode == "malformed":
            return PlainTextResponse('oops {"access_token":"LEAKED-access-token-yyyy","refresh_token":"LEAKED-refresh-token-yyyy"', 200)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
        if record is None or record["used"] or record["challenge"] != challenge or form.get("redirect_uri") != record["redirect"]:
            return JSONResponse({"error": "invalid_grant"}, 400)
        record["used"] = True
        access, refresh = self.mint()
        return JSONResponse({"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600, "scope": "read"})

    def consent(self, link: str, *, deny: bool = False, with_iss: bool | None = None) -> str:
        """The redirect the provider would send after the person approves (or declines) ``link``."""
        query = {key: values[0] for key, values in parse_qs(urlsplit(link).query).items()}
        assert query["code_challenge_method"] == "S256" and query["resource"] == MCP_URL
        assert query["response_type"] == "code" and query["state"] and query["code_challenge"]
        params = {"state": query["state"]}
        if deny:
            params["error"] = "access_denied"
        else:
            code = secrets.token_urlsafe(24)
            self.codes[code] = {"challenge": query["code_challenge"], "redirect": query["redirect_uri"], "used": False}
            params["code"] = code
        if with_iss if with_iss is not None else self.iss:
            params["iss"] = BASE
        return f"{query['redirect_uri']}?{urlencode(params)}"
