"""Telemetry for bounded advisory node-start recall.

Lookup and delivery remain in ``node_start_recall``; this module owns only the
count-only event and diagnostic shape so the query implementation stays within
its file-size limit without losing failure visibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from loguru import logger as log

from infra.product_events import emit_product_event
from memory.task_match import Match


@dataclass(frozen=True)
class Hit:
    """One recall candidate after task-term matching."""

    matched: Match
    created_at: str
    kind: str
    line: str


def log_lookup(reason: str, trigger: str, hits: list[Hit], started: float, thread_id: str) -> None:
    """Log and measure a completed recall without emitting recalled words."""
    ms = (perf_counter() - started) * 1000
    log.info("prior_words reason={} trigger={} hits={} kinds={} ms={:.1f}", reason, trigger, len(hits), _kind_counts(hits), ms)
    _emit(reason, trigger, hits, ms, thread_id)


def log_lookup_error(trigger: str, exc: Exception, started: float, thread_id: str) -> None:
    """Log and measure an advisory recall failure without affecting its caller."""
    ms = (perf_counter() - started) * 1000
    log.opt(exception=exc).warning("prior_words reason=error trigger={} hits=0 kinds=message:0,comment:0,decision:0,rule:0 ms={:.1f} error={}: {}", trigger, ms, type(exc).__name__, exc)
    _emit("error", trigger, [], ms, thread_id)


def _kind_counts(hits: list[Hit]) -> str:
    return ",".join(f"{kind}:{sum(hit.kind == kind for hit in hits)}" for kind in ("message", "comment", "decision", "rule"))


def _emit(reason: str, trigger: str, hits: list[Hit], ms: float, thread_id: str) -> None:
    """Record each lookup's outcome as counts only; no words leave the lookup."""
    emit_product_event("prior_words", reason, thread_id=thread_id, properties={
        "trigger": trigger, "hits": len(hits), "kinds": _kind_counts(hits), "ms": round(ms, 1),
    })
