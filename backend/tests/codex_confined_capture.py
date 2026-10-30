"""Credential-free real-binary capture at a loopback Responses endpoint."""
from __future__ import annotations

import asyncio
import json
import ssl
import threading
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def loopback_tls(tmp_path):
    """Trust only this fixture's ephemeral loopback server certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([
                       x509.IPAddress(ip_address("127.0.0.1")), x509.DNSName("localhost"),
                   ]), critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "loopback.pem", tmp_path / "loopback-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, cert_path


class CaptureEndpoint:
    def __init__(self, tls):
        self.requests = []
        self.unexpected = []
        self.local_services = []
        self.chatgpt_requests = 0
        self.account_checks = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                # Retain only paths and a synthetic-auth assertion, never headers.
                if self.path != "/v1/responses":
                    self.do_GET()
                    return
                assert self.headers.get("Authorization") == "Bearer synthetic-test-only"
                assert self.headers.get("ChatGPT-Account-ID") == "synthetic-account"
                owner.chatgpt_requests += 1
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "zstd":
                    import zstandard
                    with zstandard.ZstdDecompressor().stream_reader(raw) as decoded:
                        raw = decoded.read()
                body = json.loads(raw)
                owner.requests.append(body)
                event = {"type": "response.completed", "response": {
                    "id": "resp_confined_capture", "object": "response", "status": "completed",
                    "output": [], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(("event: response.completed\ndata: " + json.dumps(event) + "\n\n").encode())

            def do_GET(self):
                if self.path == "/backend-api/wham/accounts/check":
                    owner.account_checks += 1
                    body = json.dumps({"accounts": [{
                        "id": "synthetic-account", "plan_type": "pro",
                        "workspace_backend_origin": "NO_CONSTRAINT",
                        "account_routing_override": "NO_CONSTRAINT",
                    }]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/backend-api/") or self.path == "/v1/responses":
                    # Optional services and the websocket probe also stay local.
                    owner.local_services.append(self.path)
                else:
                    owner.unexpected.append(self.path)
                self.send_error(502)

            def do_CONNECT(self):
                owner.unexpected.append(self.path)  # Off-loopback HTTPS proxy attempt.
                self.send_error(502)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"https://127.0.0.1:{self.server.server_port}"

    @property
    def config(self):
        # rust-v0.162.1 model-provider-info/src/lib.rs:427-445,526-551:
        # explicit openai_base_url wins even for the built-in ChatGPT provider.
        return (
            'model_provider="openai"',
            f'openai_base_url="{self.url}/v1"',
            f'chatgpt_base_url="{self.url}/backend-api"',
        )


class AppServerClient:
    def __init__(self, session):
        self.session = session
        self.counter = 0
        self.process = None
        self.stderr = None

    async def __aenter__(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.session.build_cmd(), cwd=self.session.working_dir,
            env=self.session.build_env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        # Drain stderr to avoid deadlock, but never emit raw logs.
        self.stderr = asyncio.create_task(self.process.stderr.read())
        await self.rpc("initialize", {
            "clientInfo": {"name": "confined_capture", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await self.send({"method": "initialized"})
        return self

    async def __aexit__(self, *_args):
        if self.process.returncode is None:
            self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 10)
        await self.stderr

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 20)
        if not line:
            # This fixture has synthetic auth only and connects to loopback.
            diagnostic = (await self.stderr).decode(errors="replace")
            raise AssertionError("Codex exited before capture: " + diagnostic[:2000])
        return json.loads(line)

    async def rpc(self, method, params):
        self.counter += 1
        request_id = self.counter
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.read()
            if message.get("id") == request_id:
                assert "error" not in message, message.get("error")
                return message.get("result")

    async def complete_turn(self, thread_id):
        await self.rpc("turn/start", {"threadId": thread_id, "input": [{
            "type": "text", "text": "Run a command, read outside the grant, and search the web.",
        }]})
        while True:
            message = await self.read()
            if message.get("method") == "turn/completed":
                assert message["params"]["turn"]["status"] == "completed"
                return
