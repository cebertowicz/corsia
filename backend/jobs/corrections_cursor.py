"""Shared correction scan key and descending cursor."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingCandidateKey:
    """Ordering keys required before hydrating one classifier input."""
    rowid: int | None
    source_kind: str
    source_id: str
    thread_id: str | None
    project_id: str | None
    created_at: str


ScanCursor = tuple[str, str, str]


def before_params(before: ScanCursor | None) -> dict[str, str | None]:
    """Bind an optional descending cursor."""
    created_at, source_kind, source_id = before if before is not None else (None, None, None)
    return {"before_created_at": created_at, "before_source_kind": source_kind, "before_source_id": source_id}


def before_clause(source_kind: str, id_expression: str, created_expression: str = "created_at") -> str:
    """One descending cursor definition for every correction source."""
    return f"""(:before_created_at IS NULL
                 OR {created_expression} < :before_created_at
                 OR ({created_expression}=:before_created_at AND (
                     :source_kind < :before_source_kind
                     OR (:source_kind=:before_source_kind AND {id_expression} < :before_source_id)
                 )))"""
