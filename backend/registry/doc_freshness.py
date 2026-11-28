"""Canonical freshness rules and verification writes for shared documents."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Mapping

from infra.database import get_db, write_transaction
from registry import models
from registry.identity_metadata import merge_identity_metadata, parse_metadata
from registry.shared_principals import (
    UI_PRINCIPAL,
    acting_owner,
    artifact_owner,
    is_scope_conductor_for_shared_path,
    is_shared_document_path,
    owner_label,
    person_owner_label,
)


CADENCE_DAYS = {
    "draft": 30,
    "standard": 90,
    "prd_rfc": 30,
    "domain_reference": 30,
}

_DATED_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_VERIFIED_HEADER = re.compile(
    r"\*\*Verified at:\*\*\s*(\d{4}-\d{2}-\d{2})(?:\b|$)"
)


def _document_class(path: Path) -> tuple[str, date | None]:
    parts = {part.casefold() for part in path.parts}
    name = path.name
    upper_name = name.upper()
    if "archive" in parts:
        return "archived", None
    dated = _DATED_NAME.match(name)
    if dated:
        try:
            return "dated_evidence", date.fromisoformat(dated.group(1))
        except ValueError:
            pass
    if upper_name.startswith("DRAFT-"):
        return "draft", None
    if (
        "standards" in parts
        or any(word in upper_name for word in ("STANDARD", "CHARTER", "CONTRACT"))
    ):
        return "standard", None
    if upper_name.startswith(("PRD-", "RFC-")):
        return "prd_rfc", None
    return "domain_reference", None


def _utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def freshness(
    path: str | Path,
    metadata: Mapping[str, Any] | object | None,
    now: datetime,
) -> dict[str, Any]:
    """Return the one freshness projection for a shared-document identity."""
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    document_class, dated_at = _document_class(Path(path))
    parsed_metadata = (
        dict(metadata) if isinstance(metadata, Mapping) else parse_metadata(metadata)
    )

    if document_class == "archived":
        return {
            "class": document_class,
            "verified_at": None,
            "age_days": None,
            "due_at": None,
            "state": "archived",
        }
    if document_class == "dated_evidence":
        assert dated_at is not None
        return {
            "class": document_class,
            "verified_at": dated_at.isoformat(),
            "age_days": max(0, (current.date() - dated_at).days),
            "due_at": None,
            "state": "as_of",
        }

    verified = _utc_datetime(parsed_metadata.get("last_verified_at"))
    if verified is None:
        return {
            "class": document_class,
            "verified_at": None,
            "age_days": None,
            "due_at": None,
            "state": "never_verified",
        }
    due = verified + timedelta(days=CADENCE_DAYS[document_class])
    state = "overdue" if current >= due else (
        "draft_expiring" if document_class == "draft" else "current"
    )
    return {
        "class": document_class,
        "verified_at": verified.isoformat(),
        "age_days": max(0, (current.date() - verified.date()).days),
        "due_at": due.isoformat(),
        "state": state,
    }


# The one source for the line a read puts in front of a shared document: what it says, and the grammar that
# recognises it again (`strip_read_banner`), so a write can drop a copy of it and cannot drift from the read.
READ_BANNER_OPEN = "[Shared document · "
_PHRASES = {
    "archived": "Owner: {owner} · archived",
    "as_of": "Owner: {owner} · as of {date}",
    "never_verified": "Owner: {owner} · never verified",
    "verified": "Owner: {owner} · verified {age} {unit} ago",
    "overdue": "Owner: {owner} · verified {age} {unit} ago · review overdue",
}
_HINT = (
    "Its owner clears this after checking it against current sources with "
    '`artifact_update(artifact_id="{artifact_id}", mark_verified=true)`; '
    "editing its Verified at header does not count."
)
# What each placeholder in the phrases above can be, for any owner, date, age or artifact. An owner never holds the
# separator, so extra status text between the owner and the freshness words is not read as part of the owner.
_FIELDS = {
    "owner": r"[^\]\n·]+?", "date": r"\d{4}-\d{2}-\d{2}", "age": r"\d+", "unit": r"days?",
    "artifact_id": r'[^"\n]+',
}


def format_freshness(value: Mapping[str, Any], owner_name: str) -> str:
    """Render the compact owner/freshness phrase shared by read surfaces."""
    state = value.get("state")
    if state == "archived":
        return _PHRASES["archived"].format(owner=owner_name)
    if state == "as_of":
        return _PHRASES["as_of"].format(owner=owner_name, date=value.get("verified_at"))
    if state == "never_verified":
        return _PHRASES["never_verified"].format(owner=owner_name)
    age = int(value.get("age_days") or 0)
    unit = "day" if age == 1 else "days"
    key = "overdue" if state == "overdue" else "verified"
    return _PHRASES[key].format(owner=owner_name, age=age, unit=unit)


def verification_hint(value: Mapping[str, Any], artifact_id: str) -> str | None:
    """Tell a reader the one action that clears an unverified or overdue state.

    Verification is a registry action (DCOL-FR6.2); editing the header's
    ``Verified at`` does not count, so the hint says so.
    """
    if value.get("state") not in {"never_verified", "overdue"}:
        return None
    return _HINT.format(artifact_id=artifact_id)


def read_banner(phrase: str, hint: str | None) -> str:
    """The line a read puts first for a shared document: the phrase in brackets, then the hint when there is one."""
    banner = f"{READ_BANNER_OPEN}{phrase}]"
    return f"{banner} {hint}" if hint else banner


def _grammar(template: str) -> str:
    """The pattern for the text a template can print, each placeholder standing for any value it can take."""
    parts = re.split(r"(\{\w+\})", template)
    return "".join(f"(?:{_FIELDS[part[1:-1]]})" if part.startswith("{") else re.escape(part) for part in parts)


_READ_BANNER_LINE = re.compile(
    re.escape(READ_BANNER_OPEN) + "(?:" + "|".join(_grammar(phrase) for phrase in _PHRASES.values()) + r")\]"
    + "(?: " + _grammar(_HINT) + r")?(?:\r?\n|\Z)(?:[ \t]*\r?\n)?"
)


def strip_read_banner(text: str) -> str:
    """Drop a first line that is a read's own banner, and one blank line after it, from text about to be written.

    Only text the read could have printed matches (any owner, date, age or artifact, since a copy can come from an
    older read). A first line with anything else in its brackets, and a banner anywhere after line 1, are kept as written.
    """
    match = _READ_BANNER_LINE.match(text)
    return text[match.end():] if match else text


def artifact_freshness(
    artifact: Mapping[str, Any] | None,
    path: str | Path,
    *,
    db,
    now: datetime | None = None,
    multi_person: bool | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a display-ready freshness payload for one shared artifact.

    A person's screen passes ``multi_person`` (``document_supervisor.is_multi_person``,
    read once per request) and gets the owner as ``person_owner_label`` words it;
    an agent's read leaves it out and keeps ``owner_label``'s wording.
    """
    if artifact is None or not is_shared_document_path(path, workspace=_workspace(), db=db):
        return None
    owner = artifact_owner(str(artifact["id"]), db=db)
    if owner is None:
        return None
    value = freshness(path, artifact.get("metadata"), now or datetime.now(timezone.utc))
    value["owner_name"] = (
        owner_label(owner, db=db) if multi_person is None
        else person_owner_label(owner, db=db, user_id=user_id,
                                owner_user_id=parse_metadata(artifact.get("metadata")).get("owner_user_id"))
    )
    return value


def mark_verified(
    artifact_id: str,
    principal: str,
    *,
    db=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark one shared document verified when ``principal`` has that authority."""
    connection = db or get_db()
    try:
        artifact = models.get_artifact(artifact_id, db=connection)
        if artifact is None or artifact.get("state") != "active":
            raise ValueError("artifact not found")
        raw_path = artifact.get("path") or artifact.get("external_id")
        if not raw_path or not is_shared_document_path(
            raw_path, workspace=_workspace(), db=connection
        ):
            raise ValueError("artifact is not a registered shared document")
        owner = artifact_owner(artifact_id, db=connection)
        if owner is None:
            raise ValueError("shared document has no registry owner")
        allowed = principal == UI_PRINCIPAL or principal == owner
        if not allowed:
            allowed = acting_owner(principal, False, db=connection) == owner
        if not allowed:
            allowed = is_scope_conductor_for_shared_path(
                principal, raw_path, workspace=_workspace(), db=connection
            )
        if not allowed:
            label = owner_label(owner, db=connection)
            raise PermissionError(
                f"{Path(raw_path).name} is owned by {label}. Only its owner, "
                "the owner's worker, the conductor, or the user marks it verified."
            )

        verified_at = now or datetime.now(timezone.utc)
        if verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=timezone.utc)
        updates = {
            "last_verified_at": verified_at.astimezone(timezone.utc).isoformat(),
            "last_verified_by": principal,
        }
        with write_transaction(connection, scope="mark_document_verified") as transaction:
            merge_identity_metadata(transaction, artifact_id, updates)
        return models.get_artifact(artifact_id, db=connection) or artifact
    finally:
        if db is None:
            connection.close()


def seed_verified_from_headers(
    artifact_id: str,
    path: str | Path | None = None,
    *,
    db=None,
) -> bool:
    """Seed a missing verification timestamp from one parseable header date."""
    connection = db or get_db()
    try:
        artifact = models.get_artifact(artifact_id, db=connection)
        if artifact is None:
            return False
        raw_path = path or artifact.get("path") or artifact.get("external_id")
        if not raw_path:
            return False
        candidate = Path(raw_path)
        try:
            header = candidate.read_bytes()[:8192].decode("utf-8", errors="replace")
        except OSError:
            return False
        verified_date = None
        for line in header.splitlines()[:20]:
            if line.startswith("## "):
                break
            match = _VERIFIED_HEADER.search(line)
            if match is None:
                continue
            try:
                verified_date = date.fromisoformat(match.group(1))
            except ValueError:
                return False
            break
        if verified_date is None:
            return False
        with write_transaction(connection, scope="seed_document_verification") as transaction:
            row = transaction.execute(
                "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,)
            ).fetchone()
            if row is None or parse_metadata(row["metadata"]).get("last_verified_at"):
                return False
            timestamp = datetime.combine(
                verified_date, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat()
            merge_identity_metadata(transaction, artifact_id, {"last_verified_at": timestamp})
        return True
    finally:
        if db is None:
            connection.close()


def _workspace() -> Path:
    from infra import config

    return config.WORKSPACE
