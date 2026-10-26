"""One priceable-token contract shared by Measurement reads and their partial index."""

from __future__ import annotations


CLAUDE_CODE_SOURCE = "external_harness:claude-code"
CODEX_SOURCE = "external_harness:codex"
TOKEN_BUCKETS = ("prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens")
_REVIEW_SOURCES = "'host_access_action_review','host_access_application_review'"


def _column(alias: str, name: str) -> str:
    return f"{alias}.{name}" if alias else name


def buckets_reported_sql(alias: str = "") -> str:
    """Predicate for the retained rows whose four token buckets are usable."""
    return " AND ".join(
        f"typeof({_column(alias, bucket)})='integer' AND {_column(alias, bucket)}>=0"
        for bucket in TOKEN_BUCKETS
    )


def total_agrees_sql(alias: str = "") -> str:
    """Predicate for a reported total that agrees with the disjoint buckets."""
    total = _column(alias, "total_tokens")
    buckets = "+".join(_column(alias, bucket) for bucket in TOKEN_BUCKETS)
    return f"({total} IS NULL OR {total}={buckets})"


def contract_proven_sql(alias: str = "") -> str:
    """Predicate for writers whose retained bucket contract is proven."""
    source = _column(alias, "source")
    model = _column(alias, "model")
    usage_id = _column(alias, "id")
    created_at = _column(alias, "created_at")
    cache_creation = _column(alias, "cache_creation_tokens")
    cache_read = _column(alias, "cache_read_tokens")
    return (
        f"(CASE WHEN COALESCE({source},'') IN ('{CLAUDE_CODE_SOURCE}','checkpoint',{_REVIEW_SOURCES}) "
        f"AND COALESCE({model},'') LIKE 'claude-code/%' THEN 1 "
        f"WHEN COALESCE({source},'') IN ('{CODEX_SOURCE}','checkpoint') AND COALESCE({model},'') LIKE 'codex/%' "
        f"AND length({usage_id})=24 AND {usage_id} NOT GLOB '*[^0-9a-f]*' THEN 1 "
        f"WHEN COALESCE({source},'') IN ({_REVIEW_SOURCES}) AND COALESCE({model},'') LIKE 'codex/%' THEN 1 "
        f"WHEN COALESCE({source},'')='conversation' AND COALESCE({model},'') LIKE 'external@%' "
        f"AND {created_at}>='2026-09-17' THEN 1 "
        f"WHEN {cache_creation}=0 AND {cache_read}=0 "
        f"AND COALESCE({source},'')<>'{CODEX_SOURCE}' AND COALESCE({model},'') NOT LIKE 'codex/%' THEN 1 "
        "ELSE 0 END)=1"
    )


def priceable_usage_predicate(alias: str = "") -> str:
    """The exact partial-index predicate for a priceable retained usage row."""
    return " AND ".join((buckets_reported_sql(alias), total_agrees_sql(alias), contract_proven_sql(alias)))
