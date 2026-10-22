"""The one TLS context every mail-host connection uses: verifying, with no override.

Certificates and host names are always checked against the system's trust store. There is
no setting, argument or environment of CORSIA's own that loosens this, and no
accept-this-certificate path. A server that can't present a valid certificate is unreachable.
"""

from __future__ import annotations

import ssl


def context() -> ssl.SSLContext:
    """A fresh verifying client context (``CERT_REQUIRED``, host name checked)."""
    return ssl.create_default_context()
