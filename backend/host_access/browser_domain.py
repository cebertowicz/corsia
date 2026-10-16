"""The registrable domain a shared-browser page belongs to: the person's phishing cue (AR5).

The password step's title and the pane's address both take it from this one function, which reads the
Public Suffix List bundled with the `publicsuffixlist` package, private-domain section included, so
each `github.io` site or `co.uk` company counts as its own domain.
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache
from urllib.parse import urlparse

from publicsuffixlist import PublicSuffixList


@lru_cache(maxsize=1)
def _suffixes() -> PublicSuffixList:
    return PublicSuffixList()


def registrable_domain(host: str) -> str:
    """The host's registrable domain; a host with none (an IP, localhost, a bare suffix) stands as itself."""
    host = host.strip().rstrip(".").lower()
    if not host:
        return ""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    return _suffixes().privatesuffix(host) or host


def url_domain(url: object) -> str | None:
    """The registrable domain of a page URL, or None for a URL without a host."""
    if not isinstance(url, str):
        return None
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    return registrable_domain(host) or None if host else None
