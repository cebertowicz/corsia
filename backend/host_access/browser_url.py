"""Shared browser navigation URL validation and normalization."""
import ipaddress
import re
import socket
from urllib.parse import urlparse


def is_http_url(url: object) -> bool:
    """Accept only page-navigation URLs the shared browser can safely open."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_browser_url(text: object) -> str | None:
    """Normalize a web address while refusing non-web schemes and search text."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text == "about:blank":
        return text
    if not text or any(char.isspace() for char in text):
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        try:
            return text if is_http_url(text) else None
        except ValueError:
            return None
    # A colon after a hostname is allowed only for a numeric port; bracketed IPv6
    # is parsed separately below. This also refuses schemeless URI forms.
    authority = re.split(r"[/?#]", text, maxsplit=1)[0]
    if authority.startswith("["):
        if not re.fullmatch(r"\[[^]]+\](?::\d+)?", authority):
            return None
    elif ":" in authority and not re.fullmatch(r"[^:]+:\d+", authority):
        return None
    try:
        host = urlparse(f"https://{text}").hostname
        if not host:
            return None
        host = host.rstrip(".")
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost" or host.lower().endswith(".localhost")
            if not loopback:
                try:
                    loopback = ipaddress.ip_address(socket.inet_aton(host)).is_loopback
                except OSError:
                    pass
        if not loopback and "." not in host:
            return None
    except ValueError:
        return None
    return f"{'http' if loopback else 'https'}://{text}"
