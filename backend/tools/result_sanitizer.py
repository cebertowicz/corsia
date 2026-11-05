"""Sanitize tool results without losing connected-app or media metadata."""
from typing import Any

from host_access.credential_sanitizer import CredentialSafeText, sanitize_credential_text
from tools.mcp_stamp import restamp


def sanitize_result(result: Any) -> Any:
    """Apply the text/media boundary; read_file's CredentialSafeText is pre-redacted."""
    if isinstance(result, CredentialSafeText):
        return str(result)
    if not isinstance(result, str):
        return result
    redacted = restamp(result, sanitize_credential_text(result))
    media = getattr(result, "media", None)
    if media is None:
        return redacted
    # Redaction rewrites text; it must not strip the media reference the
    # provider boundary needs to resolve bytes.
    from core.media_reads import MediaToolResult

    return MediaToolResult(redacted, media)
