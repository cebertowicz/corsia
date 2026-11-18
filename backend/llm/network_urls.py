"""Construct HTTP origins for registered network hosts."""


def http_origin(host: str, port: int) -> str:
    """Bracket IPv6 literals when composing an HTTP authority."""
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{authority}:{port}"
