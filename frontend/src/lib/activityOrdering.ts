import type { ActivityIndexRow } from '$lib/api/plans';
import type { PlanCardState } from '$lib/components/plans/planDisplay';

/**
 * Ordering and sectioning of Activity plan rows: promotion tiers with decay,
 * the paused and in-progress predicates, and the recency sort. It reads only
 * what a row must carry, so it stays independent of the row-building module.
 */
export interface OrderableRow {
	entry: ActivityIndexRow;
	state: PlanCardState;
}

export function timestamp(value: string): number {
	const parsed = Date.parse(value);
	return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * How long a fresh exceptional-status row keeps its tier-1 promotion.
 *
 * Chosen to cover one contiguous working session: a blocker or user review raised
 * while the user is working stays on top for the rest of that session, but by
 * the next one it orders by recency and keeps only its mark. A much longer
 * window reproduces the reported defect (yesterday's blocker outranking work
 * that moved minutes ago); a much shorter one lets a live blocker slide away
 * while the user is still working on it.
 *
 * Do not "tune" this without re-reading the ordering contract in
 * `shared/planning/PLANS-CANONICAL-STATE-MATRIX.md`: decay exists so a stale
 * blocker cannot outrank live motion. Decay only ever reorders — it never
 * hides, filters, or collapses a row.
 */
export const PROMOTION_QUIET_WINDOW_MS = 4 * 60 * 60 * 1000;

/**
 * Ordering tiers, then recency within each (canonical ordering contract).
 *
 * Tier 2 exists because the rollup truncates: with only exceptional promotion,
 * recently-touched idle plans push live progress below the fold and Activity
 * stops answering "how is everything going".
 */
export type ActivityRowTier = 1 | 2 | 3;

/** Only a concrete user ask may take tier 1 while fresh. */
const PRIORITY_STATES = new Set<PlanCardState>(['needs-you']);

/**
 * When this row last became, or last moved as, the user's problem.
 *
 * Deliberately not `plan.updated_at`: that advances on any plan mutation, so a
 * blocked plan whose child keeps writing progress would refresh its own
 * promotion forever and never age out. The nodes already publish the fact we
 * want, so this stays pure presentation over server-owned fields.
 */
function prioritySince(row: OrderableRow): number {
	return timestamp(row.entry.priority_at);
}

/**
 * Paused by the plan's own state, or read as paused by liveness. Liveness
 * alone misses a paused plan that also holds a review or moving leaf.
 */
function isPaused(row: OrderableRow): boolean {
	return row.entry.plan.state === 'paused' || row.entry.progress.liveness === 'paused';
}

/**
 * Whether the row is work in progress. The server labels everything it cannot
 * call working, needs-you or finished as `waiting`, and a paused plan can still
 * carry a needs-you label, so the label alone is not enough: a paused plan is
 * never in progress.
 */
export function isInProgress(row: OrderableRow): boolean {
	return (row.state === 'working' || row.state === 'needs-you') && !isPaused(row);
}

/** The tier this row occupies right now, after promotion decay. */
export function rowTier(row: OrderableRow, now: number): ActivityRowTier {
	if (isPaused(row)) return 3;
	if (PRIORITY_STATES.has(row.state)) {
		// A decayed exceptional row leaves tier 1 entirely rather than ordering by
		// recency inside it: staying in tier 1 would still outrank live motion,
		// which is the outcome decay exists to prevent.
		return now - prioritySince(row) <= PROMOTION_QUIET_WINDOW_MS ? 1 : 3;
	}
	return row.entry.progress.liveness === 'advancing' ? 2 : 3;
}

/** Earliest instant a currently promoted row must leave tier 1. */
export function nextPromotionExpiry(groups: { current: OrderableRow[] }[], now: number): number | null {
	let next = Number.POSITIVE_INFINITY;
	for (const group of groups) {
		for (const row of group.current) {
			if (!PRIORITY_STATES.has(row.state) || rowTier(row, now) !== 1) continue;
			// `rowTier` keeps the row promoted through the exact boundary millisecond.
			next = Math.min(next, prioritySince(row) + PROMOTION_QUIET_WINDOW_MS + 1);
		}
	}
	return Number.isFinite(next) ? next : null;
}

export function sortRows<Row extends OrderableRow>(rows: Row[], now: number): Row[] {
	return rows.sort((left, right) => (
		rowTier(left, now) - rowTier(right, now)
		|| timestamp(right.entry.plan.updated_at) - timestamp(left.entry.plan.updated_at)
		|| left.entry.plan.id.localeCompare(right.entry.plan.id)
	));
}
