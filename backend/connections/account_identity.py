"""Read comparison facts without retaining vendor credentials or raw identifiers."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

CODEX_PUBLIC_USER_CLAIMS = ("https://api.openai.com/auth", "chatgpt_user_id", "user_id")


def codex_identity(home: str | Path) -> dict[str, str]:
    """Read the account fingerprint and observed login email from the Codex auth file."""
    try:
        tokens = json.loads((Path(home) / "auth.json").read_text())["tokens"]
        account = tokens["account_id"]
        token = tokens["id_token"]
        payload = token.split(".")
        if len(payload) != 3:
            return {}
        encoded = payload[1]
        claims = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
        namespace, user_key, fallback = CODEX_PUBLIC_USER_CLAIMS
        auth = claims.get(namespace)
        user = auth.get(user_key) if isinstance(auth, dict) else None
        user = user if isinstance(user, str) and user else auth.get(fallback) if isinstance(auth, dict) else None
        if not (isinstance(account, str) and account and isinstance(user, str) and user):
            return {}
        canonical = json.dumps([account, user], ensure_ascii=False, separators=(",", ":"))
        identity = {"account_fp": hashlib.sha256(canonical.encode()).hexdigest()}
        email = claims.get("email") or (auth.get("email") if isinstance(auth, dict) else None)
        if isinstance(email, str) and email:
            identity["email"] = email
        return identity
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def claude_login_status(report: object, code: int | None) -> str:
    """Classify the CLI status without retaining its account details."""
    flag = report.get("loggedIn") if isinstance(report, dict) else None
    if flag is False and code in (0, 1):
        return "signed_out"
    return "signed_in" if flag is True and code == 0 else "unknown"


# Login identity reads are account-scoped; ambient identity is only a setup hint.
