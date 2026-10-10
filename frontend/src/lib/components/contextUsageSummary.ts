// The one computation behind every "how full is this thread" number: the
// context sheet headline and the composer chip both read it.
import type { ContextUsage } from '$lib/stores/threads';
import type { ContextSheetSnapshotModel } from './contextSheetModel';

export interface ContextSheetUsageSummary {
	/** True only when the harness actually measured provider token usage. */
	hasMeasuredUsage: boolean;
	/**
	 * Provenance of usedTokens/windowTokens: "measured" | "estimated" (a
	 * reconstructed snapshot), or null when there are no numbers at all.
	 * pct/level describe either source; tokenSource keeps an estimate explicit.
	 */
	tokenSource: 'measured' | 'estimated' | null;
	usedTokens: number | null;
	windowTokens: number;
	/**
	 * The provider's real capacity when it is larger than windowTokens, i.e.
	 * when windowTokens is a CORSIA budget rather than the model's window.
	 * Null when no cap is in force and the two are the same number.
	 */
	modelWindowTokens: number | null;
	/**
	 * True when windowTokens is CORSIA's fold budget, not the model's window.
	 * An external harness keeps its own conversation and is only folded at that
	 * budget, so occupancy can legitimately exceed it (#1753) — which is why
	 * over-budget is a real state to render, not an impossible one to clamp.
	 */
	countsAgainstBudget: boolean;
	overBudget: boolean;
	utilization: number | null;
	pct: number | null;
	level: 'ok' | 'warn' | 'crit';
}

function numeric(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

/** Summarize a usage row into the used/limit/percentage the widgets render. */
export function contextSheetUsageSummary(
	usage: ContextUsage,
	_snapshot: ContextSheetSnapshotModel | null = null,
): ContextSheetUsageSummary {
	const windowTokens = Math.round(numeric(usage.model_context_window) ?? 0);
	const discoveredWindow = numeric(usage.discovered_context_window);
	// An operator cap is in force only when the provider offers strictly more
	// than CORSIA counts against. Equal values mean the counted window IS the
	// model's window, so nothing extra is worth telling the user.
	const countsAgainstBudget =
		discoveredWindow !== null && windowTokens > 0 && discoveredWindow > windowTokens;
	const modelWindowTokens = countsAgainstBudget ? Math.round(discoveredWindow) : null;
	const usageTotal = numeric(usage.total_used);
	const previewWithoutProvenance = usage.preview === true
		&& usage.token_source === undefined
		&& usage.token_usage_available === undefined;
	const explicitlyEstimated = usage.token_source === 'estimated' || previewWithoutProvenance;
	const explicitlyUnavailable = usage.token_usage_available === false && !explicitlyEstimated;
	const measured = !explicitlyUnavailable && !explicitlyEstimated && (
		usage.token_usage_available === true
		|| usage.token_source === 'measured'
		|| (usage.token_usage_available === undefined && usage.token_source === undefined && usageTotal !== null)
	);
	// Source rows are explanatory estimates. The headline always uses the
	// backend's measured/estimated total and never substitutes their sum.
	const usedTokens = explicitlyUnavailable ? null : usageTotal;
	const utilization = usedTokens !== null && windowTokens > 0
		? usedTokens / windowTokens
		: null;
	const hasNumbers = usedTokens !== null && utilization !== null && windowTokens > 0;
	// Honest provenance: a reconstructed row is estimated, never measured. Honour
	// the backend's signals (token_source, token_usage_available=false) instead of
	// presenting an estimate as a measurement.
	const tokenSource: 'measured' | 'estimated' | null = !hasNumbers
		? null
		: measured
			? 'measured'
			: explicitlyEstimated
				? 'estimated'
				: null;
	// Provenance and presentation are separate: an estimate is labelled, but a
	// numeric estimate still needs the same occupancy gauge as a measurement.
	const hasMeasuredUsage = hasNumbers && tokenSource === 'measured';
	// Report the real ratio. It used to be clamped to 100%, which hid the one
	// state worth seeing: occupancy past the fold budget on an external harness
	// (#1753). The count and the percentage now agree instead of contradicting
	// each other, and the surface says which limit it is counting against.
	const pct = hasNumbers ? Math.round((utilization ?? 0) * 100) : null;
	const overBudget = hasNumbers && (utilization ?? 0) > 1;
	const level = !hasNumbers
		? 'ok'
		: (utilization ?? 0) >= 0.9
			? 'crit'
			: (utilization ?? 0) >= 0.7
				? 'warn'
				: 'ok';
	return {
		hasMeasuredUsage,
		tokenSource,
		usedTokens,
		windowTokens,
		modelWindowTokens,
		countsAgainstBudget,
		overBudget,
		utilization,
		pct,
		level,
	};
}
