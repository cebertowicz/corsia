"""Fake IMAP, SMTP and CalDAV servers over TLS, signed by a throwaway CA, for the mail-host tests and demo.

Each server records what it was sent (``commands`` as ``(tls, verb)``; ``plain`` is every byte
received before TLS) so a test can prove no login reached it in the clear. The CA is trusted
only by the test's own client context; nothing here touches CORSIA's TLS settings.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import shlex
import socket
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HOST = "127.0.0.1"
HOST_REPLY_MARKER = "HOST-REPLY-MARKER-31337"
HEADER_FIELDS = ("from", "to", "cc", "date", "subject")


@dataclass
class Authority:
    ca_file: Path
    cert_file: Path
    key_file: Path

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca_file))

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_file), str(self.key_file))
        return context


def make_authority(directory: Path) -> Authority:
    """A CA and one server certificate for 127.0.0.1, written under ``directory``."""
    now = datetime.datetime.now(datetime.timezone.utc)
    span = dict(not_valid_before=now - datetime.timedelta(days=1), not_valid_after=now + datetime.timedelta(days=2))
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CORSIA test CA")])

    def build(subject: x509.Name, key, issuer_key, *, ca: bool, san: bool) -> x509.Certificate:
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(span["not_valid_before"])
                   .not_valid_after(span["not_valid_after"])
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False))
        if san:
            import ipaddress

            builder = builder.add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address(HOST))]), critical=False)
        return builder.sign(issuer_key, hashes.SHA256())

    ca_cert = build(ca_name, ca_key, ca_key, ca=True, san=False)
    leaf = build(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]), leaf_key, ca_key, ca=False, san=True)
    pem = serialization.Encoding.PEM
    files = Authority(directory / "ca.pem", directory / "server.pem", directory / "server.key")
    files.ca_file.write_bytes(ca_cert.public_bytes(pem))
    files.cert_file.write_bytes(leaf.public_bytes(pem))
    files.key_file.write_bytes(leaf_key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return files


class _Connection:
    def __init__(self, sock: socket.socket) -> None:
        self.sock, self.buffer, self.tls = sock, b"", False

    def readline(self) -> bytes | None:
        while b"\r\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\r\n")
        return line

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def send(self, *lines: str | bytes) -> None:
        for line in lines:
            self.sock.sendall((line.encode() if isinstance(line, str) else line) + b"\r\n")

    def upgrade(self, context: ssl.SSLContext) -> None:
        self.sock, self.buffer, self.tls = context.wrap_socket(self.sock, server_side=True), b"", True


class _LineServer:
    """A threaded TCP server: implicit TLS, or plain with optional STARTTLS."""

    def __init__(self, authority: Authority, *, implicit_tls: bool) -> None:
        self.authority, self.implicit_tls = authority, implicit_tls
        self.commands: list[tuple[bool, str]] = []
        self.plain = b""
        self._listener = socket.socket()
        self._listener.bind((HOST, 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw: socket.socket) -> None:
        conn = _Connection(raw)
        try:
            if self.implicit_tls:
                conn.upgrade(self.authority.server_context())
            self.session(conn)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                conn.sock.close()
            except OSError:
                pass

    def read(self, conn: _Connection) -> str | None:
        line = conn.readline()
        if line is not None and not conn.tls:
            self.plain += line
        return line.decode("utf-8", "replace") if line is not None else None

    def session(self, conn: _Connection) -> None:
        raise NotImplementedError

    def close(self) -> None:
        # Linux does not wake a thread blocked in accept() when the listener is only closed.
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        self._listener.close()


class FakeImap(_LineServer):
    def __init__(self, authority: Authority, *, user: str, password: str, messages: dict[int, bytes] | None = None,
                 implicit_tls: bool = True, offer_starttls: bool = False, imap_off: bool = False, validity: int = 7,
                 folders: dict[str, str] | None = None) -> None:
        self.user, self.password, self.messages = user, password, dict(messages or {})
        self.offer_starttls, self.imap_off, self.validity = offer_starttls, imap_off, validity
        self.searches: list[str] = []
        self.appended: list[tuple[str, bytes]] = []
        #: Folder name -> its RFC 6154 flags, as LIST shows them.
        self.folders = {"INBOX": "", "Drafts": "\\Drafts", "Sent": "\\Sent"} if folders is None else folders
        super().__init__(authority, implicit_tls=implicit_tls)

    def session(self, conn: _Connection) -> None:
        conn.send("* OK IMAP ready")
        while (line := self.read(conn)) is not None:
            tag, _, rest = line.partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            self.commands.append((conn.tls, verb))
            if verb == "CAPABILITY":
                conn.send("* CAPABILITY IMAP4rev1" + (" STARTTLS" if self.offer_starttls and not conn.tls else ""), f"{tag} OK done")
            elif verb == "STARTTLS" and self.offer_starttls and not conn.tls:
                conn.send(f"{tag} OK begin TLS")
                conn.upgrade(self.authority.server_context())
            elif verb == "LOGIN":
                given = shlex.split(args)
                if self.imap_off:
                    conn.send(f"{tag} NO [ALERT] IMAP access is disabled for this account {HOST_REPLY_MARKER}")
                elif given == [self.user, self.password]:
                    conn.send(f"{tag} OK logged in")
                else:
                    conn.send(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials {HOST_REPLY_MARKER}")
            elif verb in ("SELECT", "EXAMINE"):
                conn.send(f"* {len(self.messages)} EXISTS", f"* OK [UIDVALIDITY {self.validity}] ok", f"{tag} OK [READ-ONLY] done")
            elif verb == "UID":
                self._uid(conn, tag, args)
            elif verb == "LIST":
                conn.send(*(f'* LIST ({flags}) "/" "{name}"' for name, flags in self.folders.items()), f"{tag} OK done")
            elif verb == "APPEND":
                folder, _, size = args.rpartition(" ")
                conn.send("+ ready")
                raw = conn.read_exact(int(size.strip("{}+")))
                conn.readline()
                name = shlex.split(folder)[0]
                if name in self.folders:
                    self.appended.append((name, raw))
                    conn.send(f"{tag} OK appended")
                else:
                    conn.send(f"{tag} NO [TRYCREATE] no such folder")
            elif verb == "LOGOUT":
                conn.send("* BYE", f"{tag} OK bye")
                return
            else:
                conn.send(f"{tag} OK done")

    def _uid(self, conn: _Connection, tag: str, args: str) -> None:
        sub, _, rest = args.partition(" ")
        if sub.upper() == "SEARCH":
            self.searches.append(rest)
            conn.send("* SEARCH " + " ".join(str(uid) for uid in sorted(self.messages)), f"{tag} OK done")
            return
        wanted, _, items = rest.partition(" ")
        for uid in (int(part) for part in wanted.split(",")):
            raw = self.messages.get(uid)
            if raw is None:
                continue
            headers_only = "HEADER.FIELDS" in items
            data = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if headers_only else raw
            section = "BODY[HEADER.FIELDS (FROM TO CC DATE SUBJECT)]" if headers_only else "BODY[]<0>"
            conn.send(f"* {uid} FETCH (UID {uid} {section} {{{len(data)}}}".encode() + b"\r\n" + data + b")")
        conn.send(f"{tag} OK done")


class FakeSmtp(_LineServer):
    def __init__(self, authority: Authority, *, user: str, password: str, implicit_tls: bool = True,
                 offer_starttls: bool = False, plain_auth_without_tls: bool = False) -> None:
        self.user, self.password = user, password
        self.offer_starttls, self.plain_auth_without_tls = offer_starttls, plain_auth_without_tls
        self.sent: list[tuple[list[str], bytes]] = []
        super().__init__(authority, implicit_tls=implicit_tls)

    def session(self, conn: _Connection) -> None:
        conn.send("220 localhost ESMTP")
        recipients: list[str] = []
        while (line := self.read(conn)) is not None:
            verb, _, args = line.partition(" ")
            verb = verb.upper()
            self.commands.append((conn.tls, verb))
            if verb in ("EHLO", "HELO"):
                lines = ["250-localhost"]
                if self.offer_starttls and not conn.tls:
                    lines.append("250-STARTTLS")
                if conn.tls or self.plain_auth_without_tls:
                    lines.append("250-AUTH PLAIN")
                conn.send(*lines, "250 OK")
            elif verb == "STARTTLS" and self.offer_starttls and not conn.tls:
                conn.send("220 ready")
                conn.upgrade(self.authority.server_context())
            elif verb == "AUTH":
                decoded = base64.b64decode(args.split(" ", 1)[1]).split(b"\0") if " " in args else []
                ok = decoded == [b"", self.user.encode(), self.password.encode()]
                conn.send("235 accepted" if ok else f"535 rejected {HOST_REPLY_MARKER}")
            elif verb == "RCPT":
                recipients.append(args.split(":", 1)[1].strip(" <>"))
                conn.send("250 OK")
            elif verb == "DATA":
                conn.send("354 go ahead")
                lines: list[bytes] = []
                while (data_line := conn.readline()) not in (None, b"."):
                    lines.append(data_line)
                self.sent.append((recipients[:], b"\r\n".join(lines)))
                recipients.clear()
                conn.send("250 queued")
            elif verb == "QUIT":
                conn.send("221 bye")
                return
            else:
                conn.send("250 OK")


class FakeCalDav:
    """An HTTPS CalDAV server: principal, calendar home, one or more calendars, canned events."""

    def __init__(self, authority: Authority, *, user: str, password: str, events: str = "",
                 calendars: tuple[str, ...] = ("/calendars/home/work/",), principal_href: str = "/principal/",
                 supports: str = "VEVENT") -> None:
        self.requests: list[tuple[str, str, str]] = []
        #: Every PUT: ``(path, body)``; ``put_status`` is what the host answers.
        self.puts: list[tuple[str, str]] = []
        self.put_status = 201
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                given = self.headers.get("Authorization", "")
                try:
                    login = base64.b64decode(given.split(" ", 1)[1]).decode() if given.startswith("Basic ") else ""
                except ValueError:
                    login = ""
                outer.requests.append((self.command, self.path, login.partition(":")[0]))
                if login != f"{user}:{password}":
                    return self._send(401, "")
                self._send(207, outer.body(self.command, self.path))

            do_PROPFIND = do_REPORT = _answer  # noqa: N815

            def do_PUT(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                given = self.headers.get("Authorization", "")
                login = base64.b64decode(given.split(" ", 1)[1]).decode() if given.startswith("Basic ") else ""
                if login != f"{user}:{password}":
                    return self._send(401, "")
                outer.puts.append((self.path, body))
                self._send(outer.put_status, "")

            def _send(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.events, self.calendars, self.principal, self.supports = events, calendars, principal_href, supports
        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.socket = authority.server_context().wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.url = f"https://{HOST}:{self.port}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def body(self, method: str, path: str) -> str:
        ns = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'

        def reply(*responses: tuple[str, str]) -> str:
            parts = "".join(f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
                            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>" for href, props in responses)
            return f'<?xml version="1.0"?><d:multistatus {ns}>{parts}</d:multistatus>'

        if method == "REPORT":
            return reply(*((f"{path}event.ics", f"<c:calendar-data>{self.events}</c:calendar-data>"),))
        if path == "/":
            return reply(("/", f"<d:current-user-principal><d:href>{self.principal}</d:href></d:current-user-principal>"))
        if path == self.principal:
            return reply((path, "<c:calendar-home-set><d:href>/calendars/home/</d:href></c:calendar-home-set>"))
        kind = ('<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set>'
                f'<c:comp name="{self.supports}"/></c:supported-calendar-component-set>')
        return reply((path, "<d:resourcetype><d:collection/></d:resourcetype>"), *((href, kind) for href in self.calendars))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def message(subject: str, body: str, *, sender: str = "sam@example.com", html: bool = False) -> bytes:
    kind = "text/html" if html else "text/plain"
    return (f"From: Sam <{sender}>\r\nTo: you@example.com\r\nDate: Wed, 21 Oct 2026 09:00:00 -0700\r\n"
            f"Subject: {subject}\r\nContent-Type: {kind}; charset=utf-8\r\n\r\n{body}\r\n").encode()


SAMPLE_EVENTS = "\n".join((
    "BEGIN:VCALENDAR", "VERSION:2.0",
    "BEGIN:VEVENT", "UID:timed-1", "DTSTART:20261022T160000Z", "DTEND:20261022T170000Z", "SUMMARY:Standup",
    "ATTENDEE;CN=Sam:mailto:sam@example.com", "END:VEVENT",
    "BEGIN:VEVENT", "UID:holiday-1", "DTSTART;VALUE=DATE:20261023", "DTEND;VALUE=DATE:20261024", "SUMMARY:Holiday",
    "TRANSP:TRANSPARENT", "END:VEVENT",
    "BEGIN:VEVENT", "UID:weekly-1", "DTSTART:20261019T180000Z", "DTEND:20261019T183000Z", "RRULE:FREQ=WEEKLY;COUNT=4",
    "SUMMARY:Weekly sync", "END:VEVENT", "END:VCALENDAR",
))
