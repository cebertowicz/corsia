"""Automatic cleanup of merged or idle linked worktrees under ``<repo>/.worktrees/``.

``reap`` is the one entry point. Two existing triggers call it and it adds no
loop of its own: the ``pr_merged`` router hook (``worktree_reaper_pr``) and a
step in the nightly maintenance job (``reap_all_idle``). It runs in the
backend process, never inside an agent sandbox. Every check fails closed and
the first failed check records the skip reason; removal is quarantine-first
(``worktree_reaper_remove``). Each run emits one ``worktree_reaper.run_completed``
product event per repository; that metric is the whole escalation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from pathlib import Path

from loguru import logger

from infra.product_events import emit_product_event
from infra.project_repo_policy import RepoPolicy, list_registered_repo_policies
from infra.repo_flock import RepoBusyError, repo_lock
from infra.worktree_reaper_checks import (
    CheckContext,
    RemovalVerdict,
    candidate_verdict,
    orphan_skip_reason,
    recheck_before_removal,
    status_at,
)
from infra.worktree_reaper_probe import ProcessProbe, ReaperGitError, generated_trees
from infra.worktree_reaper_remove import quarantine_entries, remove_orphan, remove_worktree, sweep_quarantine
from infra.worktree_reaper_salvage import SalvageError, salvage_worktree
from infra.worktree_reaper_scan import Candidate, Orphan, scan_repo
from infra.worktree_reaper_store import forget_pr_terminal, pr_terminal_heads, remember_pr_terminal

IDLE_THRESHOLD_SECONDS = 7 * 86400
PUBLISHED_IDLE_THRESHOLD_SECONDS = 86400
MAX_REMOVALS_PER_RUN = 100
RUN_BUDGET_SECONDS = 600.0
METRIC_ITEM_CAP = 50
REMAINING_WARNING = 50


@dataclass
class RunBudget:
    """Per-run caps shared by every repository in one trigger."""

    removals_left: int = MAX_REMOVALS_PER_RUN
    deadline: float = field(default_factory=lambda: time.monotonic() + RUN_BUDGET_SECONDS)

    def exhausted(self) -> bool:
        return self.removals_left <= 0 or time.monotonic() >= self.deadline


@dataclass
class Skip:
    name: str
    owner_thread: str | None
    reason: str
    idle_days: float | None
    detail: str | None = None


@dataclass
class Salvaged:
    name: str
    owner_thread: str | None
    destination: str


@dataclass
class RepoReport:
    """Outcome of one reaper pass over one repository."""

    repo: str
    trigger: str
    scanned: int = 0
    removed: int = 0
    salvaged: list[Salvaged] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)  # gone owners' trees, nothing copied
    orphan_removed: int = 0
    quarantine_removed: int = 0
    quarantine_entries: list[str] = field(default_factory=list)
    error: str | None = None
    skips: list[Skip] = field(default_factory=list)

    def skip(self, name: str, owner: str | None, reason: str,
             idle_days: float | None = None, detail: str | None = None) -> None:
        self.skips.append(Skip(name, owner, reason, idle_days, detail))

    @property
    def remaining_count(self) -> int:
        return self.scanned - self.removed

    def skip_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.skips:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def _idle_days(idle_since: float, now: float) -> float:
    return round(max(now - idle_since, 0.0) / 86400, 2)


def emit_report(report: RepoReport) -> None:
    """Emit the monitoring metric and warn on failure, backlog or stuck quarantine."""
    counts = report.skip_counts()
    entries = report.quarantine_entries
    ages = [item.idle_days for item in report.skips if item.idle_days is not None]
    items = [
        {"name": item.name, "owner_thread": item.owner_thread,
         "reason": item.reason, "idle_days": item.idle_days,
         **({"detail": item.detail} if item.detail else {})}
        for item in report.skips[:METRIC_ITEM_CAP]
    ]
    emit_product_event("worktree_reaper", "run_completed", properties={
        "repo": report.repo,
        "trigger": report.trigger,
        "scanned": report.scanned,
        "removed": report.removed,
        "salvaged": len(report.salvaged),
        "salvaged_to": [asdict(item) for item in report.salvaged[:METRIC_ITEM_CAP]],
        "discarded": report.discarded[:METRIC_ITEM_CAP],
        "remaining_count": report.remaining_count,
        "orphan_removed": report.orphan_removed,
        "quarantine_removed": report.quarantine_removed,
        "quarantine_entry_count": len(entries),
        "quarantine_entries": entries[:METRIC_ITEM_CAP],
        "error": report.error,
        "skip_counts": counts,
        "oldest_skipped_idle_days": max(ages) if ages else None,
        "skipped": items,
        "skipped_truncated": max(len(report.skips) - METRIC_ITEM_CAP, 0),
    })
    if report.error or report.remaining_count > REMAINING_WARNING or entries:
        logger.warning(
            "worktree_reaper: {} error={} remaining={} quarantine_entries={} skips={}",
            report.repo, report.error, report.remaining_count, len(entries), counts,
        )


def _remove_candidate(
    repo: Path, report: RepoReport, ctx: CheckContext, candidate: Candidate,
    idle_days: float, expected_head: str | None, merged: bool,
) -> bool:
    """Recheck and remove one candidate under the repo lock; False on repo_busy."""
    owner = candidate.owner_thread
    copies: list[Path] = []
    try:
        with repo_lock(repo):
            verdict = recheck_before_removal(
                ctx, candidate, IDLE_THRESHOLD_SECONDS, expected_head, merged=merged,
                short_threshold_seconds=PUBLISHED_IDLE_THRESHOLD_SECONDS,
            )
            def clear(quarantined: Path) -> str | None:
                if verdict is RemovalVerdict.DISCARD:  # the owner is gone; nothing is copied out
                    return None
                if verdict is not RemovalVerdict.SALVAGE:  # clean; re-run the check that cleared it
                    return status_at(ctx, candidate, quarantined, merged)
                copies.append(salvage_worktree(
                    repo, quarantined, candidate.admin, candidate.name, ctx.now,
                    candidate.path, ctx.generated,
                ))
                return None

            outcome = verdict if isinstance(verdict, str) else remove_worktree(
                repo, candidate.path, candidate.name, clear)
    except RepoBusyError:
        report.skip(candidate.name, owner, "repo_busy", idle_days)
        return False
    except SalvageError as exc:
        report.skip(candidate.name, owner, "salvage_failed", idle_days, str(exc)[:240])
        return True
    except (OSError, ReaperGitError) as exc:
        # The text is the point: a timeout and a refusal need different fixes.
        logger.warning("worktree_reaper: {} remove_error: {}", candidate.name, exc)
        report.skip(candidate.name, owner, "remove_error", idle_days, str(exc)[:240])
        return True
    finally:
        # A finished copy is reported even when removal then fails or waits.
        for destination in copies:
            logger.info("worktree_reaper: salvaged {} to {}", candidate.name, destination)
            report.salvaged.append(Salvaged(candidate.name, owner, str(destination)))
    if outcome == "removed":
        report.removed += 1
        if verdict is RemovalVerdict.DISCARD:
            logger.info("worktree_reaper: discarded {} (owner {} gone)", candidate.name, owner)
            report.discarded.append(candidate.name)
        forget_pr_terminal(candidate.path)
    else:
        reason = "quarantine_removal_pending" if outcome == "quarantine_pending" else outcome
        report.skip(candidate.name, owner, reason, idle_days)
    return True


def _reap_orphans(
    repo: Path, report: RepoReport, ctx: CheckContext, orphans: list[Orphan],
    budget: RunBudget,
) -> None:
    for orphan in orphans:
        if budget.exhausted():
            report.skip(orphan.name, None, "orphan_kept", None, "cap_reached")
            continue
        reason, idle_days = orphan_skip_reason(ctx, orphan, IDLE_THRESHOLD_SECONDS)
        if reason is None:
            try:
                with repo_lock(repo):
                    reason = orphan_skip_reason(ctx, orphan, IDLE_THRESHOLD_SECONDS)[0]
                    outcome = reason or remove_orphan(repo, orphan.path)
            except RepoBusyError:
                outcome = "repo_busy"
            except OSError as exc:
                outcome = f"remove_error: {exc}"[:240]
            if outcome == "orphan_removed":
                report.orphan_removed += 1
                budget.removals_left -= 1
                continue
            if outcome == "quarantine_pending":
                report.skip(orphan.name, None, "quarantine_removal_pending", idle_days)
                continue
            reason = outcome
        report.skip(orphan.name, None, "orphan_kept", idle_days, reason)


@dataclass
class _Queued:
    """One candidate the checks cleared for removal, waiting its turn in this pass."""

    candidate: Candidate
    idle_days: float
    expected_head: str | None
    merged: bool


def _skip_unchecked(report: RepoReport, rest: list[Candidate], now: float, reason: str) -> None:
    for candidate in rest:
        report.skip(candidate.name, candidate.owner_thread, reason,
                    _idle_days(candidate.idle_since, now))


def _skip_queued(report: RepoReport, rest: list[_Queued], reason: str) -> None:
    for queued in rest:
        report.skip(queued.candidate.name, queued.candidate.owner_thread, reason, queued.idle_days)


def _walk_candidates(
    repo: Path, report: RepoReport, ctx: CheckContext, budget: RunBudget, mode: str,
    pr_sha: str | None, deferred: dict[str, str], selected: list[Candidate],
) -> tuple[list[_Queued], bool]:
    """Judge and remove candidates in idle order, cheapest reclaim first.

    Returns the salvage queue and whether the repository lock stopped the walk;
    candidates this walk did not reach are reported here. The order decides which
    candidates a budget-cut pass reaches, never what may be removed: each removal
    still rechecks under the lock.
    """
    queue: list[_Queued] = []
    unchecked = list(selected)
    while unchecked and not budget.exhausted():
        candidate = unchecked.pop(0)
        idle_days = _idle_days(candidate.idle_since, ctx.now)
        head = candidate.entry.head if candidate.entry else None
        recorded_head = deferred.get(str(candidate.path))
        if mode == "idle" and recorded_head is not None and recorded_head != head:
            forget_pr_terminal(candidate.path)
            deferred.pop(str(candidate.path))
        merged = mode == "pr_terminal" or (head is not None and recorded_head == head)
        expected_head = pr_sha if mode == "pr_terminal" else deferred.get(str(candidate.path))
        verdict = candidate_verdict(
            ctx, candidate, IDLE_THRESHOLD_SECONDS, merged=merged,
            short_threshold_seconds=PUBLISHED_IDLE_THRESHOLD_SECONDS,
        )
        if isinstance(verdict, str) and mode == "pr_terminal" and head is not None:
            # Blocked at merge time (a live process, a Git lock, uncommitted
            # edits): the nightly pass retries it under the same merged rules.
            remember_pr_terminal(candidate.path, head)
        if isinstance(verdict, str):
            report.skip(candidate.name, candidate.owner_thread, verdict, idle_days)
            continue
        if verdict is RemovalVerdict.SALVAGE:
            queue.append(_Queued(candidate, idle_days, expected_head, merged))
            continue
        removed_before = report.removed
        if not _remove_candidate(repo, report, ctx, candidate, idle_days, expected_head, merged):
            _skip_unchecked(report, unchecked, ctx.now, "repo_busy")
            return queue, True
        budget.removals_left -= report.removed - removed_before
    _skip_unchecked(report, unchecked, ctx.now, "cap_reached")
    return queue, False


def _drain_salvage_queue(
    repo: Path, report: RepoReport, ctx: CheckContext, budget: RunBudget, queue: list[_Queued],
) -> None:
    """Spend what is left of the budget on the candidates that need a copy-out.

    Their verdicts were computed in the walk, so the wait changes only the order.
    """
    for index, queued in enumerate(queue):
        if budget.exhausted():
            _skip_queued(report, queue[index:], "cap_reached")
            return
        # Re-read open PRs here, not under the repo lock the recheck holds.
        ctx.open_pr_branches(fresh=True)
        removed_before = report.removed
        if not _remove_candidate(
            repo, report, ctx, queued.candidate, queued.idle_days,
            queued.expected_head, queued.merged,
        ):
            _skip_queued(report, queue[index + 1:], "repo_busy")
            return
        budget.removals_left -= report.removed - removed_before


def reap(
    policy: RepoPolicy,
    *,
    mode: str,
    candidates: list[Path] | None = None,
    budget: RunBudget | None = None,
    probe: ProcessProbe | None = None,
    pr_sha: str | None = None,
) -> RepoReport:
    """Reap one repository. ``mode`` is ``idle`` or ``pr_terminal``.

    ``candidates`` restricts the pass to those worktree paths (the PR hook's
    mapped worktree), and ``pr_sha`` is the merged head that the worktree's
    HEAD must still be contained in at removal time. The idle pass also
    sweeps quarantine and orphans. Every report lists what is still quarantined.
    """
    repo = policy.path.resolve(strict=False)
    report = RepoReport(repo=str(repo), trigger=mode)
    budget = budget or RunBudget()
    if (repo / ".git").is_symlink() or not (repo / ".git").is_dir():
        report.skip(repo.name, None, "repo_layout")
        return report
    ctx = CheckContext(repo, probe or ProcessProbe(), time.time(), generated_trees(repo))
    found, orphans, _registrations = scan_repo(repo)
    wanted = {path.resolve(strict=False) for path in candidates or []}
    selected = [item for item in found if candidates is None or item.path in wanted]
    selected.sort(key=lambda item: item.idle_since)
    report.scanned = len(selected)
    deferred = pr_terminal_heads()
    salvage_queue, halted = _walk_candidates(
        repo, report, ctx, budget, mode, pr_sha, deferred, selected)
    if mode == "idle":  # after the cheap removals, before the slow copies
        report.quarantine_removed = sweep_quarantine(repo, budget)
    if halted:  # the repository lock is held elsewhere; nothing else can be removed
        _skip_queued(report, salvage_queue, "repo_busy")
    else:
        _drain_salvage_queue(repo, report, ctx, budget, salvage_queue)
    if mode == "idle":
        _reap_orphans(repo, report, ctx, orphans, budget)
    report.quarantine_entries = quarantine_entries(repo)
    return report


def _enforceable(policy: RepoPolicy) -> bool:
    return policy.source_binding_valid and not policy.enforcement_error


def failed_report(repo: str, trigger: str, reason: str, exc: BaseException) -> RepoReport:
    """Return the report for a pass that failed before or during its checks."""
    report = RepoReport(repo=repo, trigger=trigger)
    report.error = f"{reason}: {exc.__class__.__name__}: {exc}"[:240]
    return report


def reap_all_idle() -> list[RepoReport]:
    """Nightly step: reap every valid registered repository once.

    Never raises. A failure before any repository is reached, such as the
    policy read, still emits one ``run_completed`` event carrying ``error``,
    so a whole-pass failure is never just a missing event.
    """
    reports: list[RepoReport] = []
    try:
        budget = RunBudget()
        probe = ProcessProbe()
        policies = [p for p in list_registered_repo_policies() if _enforceable(p)]
    except Exception as exc:
        logger.exception("worktree_reaper: idle pass could not start")
        report = failed_report("", "idle", "pass_error", exc)
        emit_report(report)
        return [report]
    for policy in policies:
        try:
            report = reap(policy, mode="idle", budget=budget, probe=probe)
        except Exception as exc:
            logger.exception("worktree_reaper: idle pass failed for {}", policy.path)
            report = failed_report(str(policy.path), "idle", "repo_error", exc)
        emit_report(report)
        reports.append(report)
    return reports
