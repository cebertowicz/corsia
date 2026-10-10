import { get, writable } from 'svelte/store';
import { measurementScope, type MeasurementScope } from './measurement';
import { grainScopeParams, measurementGrain, type MeasurementGrain } from './measurementGrain';
import { decodeAveragePeriods, decodePeriod, type MeasurementPeriod } from './measurementPeriod';

// Cost counts represent retained ledger usage records.
export type CostPeriod = {
	period: string;
	dollars: number | null;
	priced_records: number;
	api_records: number;
	outcomes: number;
	per_outcome: number | null;
	/** Size-rated accepted outcomes and their hours of work; per_hour is all priced spend over those hours, withheld under the minimum n. */
	sized: number;
	hours: number;
	per_hour: number | null;
	/** The moving average of per_hour (7 days or 4 weeks ending here); null before the series has that many periods. */
	average: number | null;
};
/** One pooled comparison window as the Cost payload serves it: the first and last day it covers and its own totals. */
export type CostWindowFigures = {
	from: string;
	to: string;
	dollars: number | null;
	priced_records: number;
	api_records: number;
	outcomes: number;
	per_outcome: number | null;
	sized: number;
	hours: number;
	per_hour: number | null;
};
export type ExcludedRecords = { kind: string; text: string; records: number };
export type WorkspaceSubscriptionContext = { subscription_dollars: number | null; overage_dollars: number | null; subscription_facts: number; overage_facts: number; subscription_coverage: 'none' | 'absent' | 'partial' | 'complete'; overage_coverage: 'none' | 'absent' | 'partial' | 'complete'; overlapping_overage: { amount_cents: number; from_date: string; through: string }[]; allocation: 'unavailable'; overage_unrecorded?: boolean; subscription_unrecorded_accounts?: number };
export type ActualCost = { subscription_records: number; unknown_charge_records: number; external_unset_records: number; direct_api_dollars: number; complete: boolean; workspace_subscription: WorkspaceSubscriptionContext | null };
export type MeasurementCost = {
	definition_version: 1;
	/** The periods the response answered for; null when it served none. */
	period: MeasurementPeriod | null;
	/** How many periods each served moving average spans. */
	averagePeriods?: number;
	series: CostPeriod[]; latest: CostWindowFigures | null; previous: CostWindowFigures | null;
	coverage: { priced_records: number; api_records: number }; not_api_priced_records: number; excluded: ExcludedRecords[]; actual: ActualCost;
};
export type CostState = { loading: boolean; error: string | null; data: MeasurementCost | null; scopeKey: string | null; grain: MeasurementGrain | null };

const COST_LOAD_ERROR = 'Could not load Cost.';
const scopeKey = (scope: MeasurementScope) => `${scope.project}:${scope.agent ?? 'none'}`;
let controller: AbortController | null = null;

export const measurementCost = writable<CostState>({ loading: true, error: null, data: null, scopeKey: null, grain: null });

/** The one wording for how much of a period's retained usage the dollars cover. */
export function coverageText(priced: number, total: number): string {
	return `${priced.toLocaleString('en-US')} of ${total.toLocaleString('en-US')} usage records priced`;
}

function numberOrNull(value: unknown): number | null { return typeof value === 'number' && Number.isFinite(value) ? value : null; }
const count = (value: unknown): number => { const parsed = numberOrNull(value); if (parsed === null) throw new Error('cost count shape'); return parsed; };
function civilDate(value: unknown): string | null {
	if (typeof value !== 'string') return null;
	const parsed = new Date(`${value}T00:00:00.000Z`);
	return Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value ? null : value;
}

/** Reject incomplete API data rather than make missing dollars look like zero. */
export function decodeMeasurementCost(value: unknown): MeasurementCost {
	const body = value as Record<string, unknown>;
	if (body.definition_version !== 1 || !Array.isArray(body.series) || !Array.isArray(body.excluded) || !body.actual || typeof body.actual !== 'object' || !body.coverage || typeof body.coverage !== 'object') throw new Error('cost response shape');
	const period = (item: unknown): CostPeriod => {
		const row = item as Record<string, unknown>;
		const dollars = row.dollars === null ? null : numberOrNull(row.dollars);
		if (typeof row.period !== 'string' || dollars === null && row.dollars !== null) throw new Error('cost period shape');
		const perOutcome = row.per_outcome === null ? null : numberOrNull(row.per_outcome);
		if (row.per_outcome !== null && perOutcome === null || dollars === null && perOutcome !== null) throw new Error('cost per-outcome shape');
		// The moving average is absent before the series is long enough; a present value that is not a number rejects the response.
		const average = row.average === undefined || row.average === null ? null : numberOrNull(row.average);
		if (row.average !== undefined && row.average !== null && average === null) throw new Error('cost average shape');
		return { period: row.period, dollars, priced_records: count(row.priced_records), api_records: count(row.api_records), outcomes: count(row.outcomes), per_outcome: perOutcome, sized: numberOrNull(row.sized) ?? 0, hours: numberOrNull(row.hours) ?? 0, per_hour: numberOrNull(row.per_hour), average };
	};
	const pooled = (item: unknown): CostWindowFigures => {
		const row = item as Record<string, unknown>;
		const dollars = row.dollars === null ? null : numberOrNull(row.dollars);
		const perOutcome = row.per_outcome === null ? null : numberOrNull(row.per_outcome);
		if (civilDate(row.from) === null || civilDate(row.to) === null || dollars === null && row.dollars !== null || row.per_outcome !== null && perOutcome === null || dollars === null && perOutcome !== null) throw new Error('cost window shape');
		return { from: row.from as string, to: row.to as string, dollars, priced_records: count(row.priced_records), api_records: count(row.api_records), outcomes: count(row.outcomes), per_outcome: perOutcome, sized: numberOrNull(row.sized) ?? 0, hours: numberOrNull(row.hours) ?? 0, per_hour: numberOrNull(row.per_hour) };
	};
	const actual = body.actual as Record<string, unknown>;
	const coverage = body.coverage as Record<string, unknown>;
	// Only All work serves the workspace bill; a project or agent view carries null.
	const workspace = actual.workspace_subscription as Record<string, unknown> | null | undefined;
	if (workspace !== null && (!workspace || typeof workspace !== 'object')) throw new Error('actual cost shape');
	const coverageKind = (value: unknown): WorkspaceSubscriptionContext['subscription_coverage'] => value === 'none' || value === 'absent' || value === 'partial' || value === 'complete' ? value : (() => { throw new Error('actual cost shape'); })();
	const moneyOrNull = (value: unknown): number | null => value === null ? null : numberOrNull(value);
	const overlappingOverage = (value: unknown): WorkspaceSubscriptionContext['overlapping_overage'] => {
		if (!Array.isArray(value)) throw new Error('actual cost shape');
		return value.map((item) => {
			if (!item || typeof item !== 'object') throw new Error('actual cost shape');
			const row = item as Record<string, unknown>;
			const amount = row.amount_cents, from = civilDate(row.from_date), through = civilDate(row.through);
			if (typeof amount !== 'number' || !Number.isFinite(amount) || !Number.isInteger(amount) || amount < 0 || !from || !through || through < from) throw new Error('actual cost shape');
			return { amount_cents: amount, from_date: from, through };
		});
	};
	if (numberOrNull(actual.direct_api_dollars) === null || typeof actual.complete !== 'boolean'
		|| workspace && (moneyOrNull(workspace.subscription_dollars) === null && workspace.subscription_dollars !== null
		|| moneyOrNull(workspace.overage_dollars) === null && workspace.overage_dollars !== null
		|| workspace.allocation !== 'unavailable')) throw new Error('actual cost shape');
	const workspaceContext: WorkspaceSubscriptionContext | null = !workspace ? null : { subscription_dollars: moneyOrNull(workspace.subscription_dollars), overage_dollars: moneyOrNull(workspace.overage_dollars), subscription_facts: count(workspace.subscription_facts), overage_facts: count(workspace.overage_facts), subscription_coverage: coverageKind(workspace.subscription_coverage), overage_coverage: coverageKind(workspace.overage_coverage), overlapping_overage: overlappingOverage(workspace.overlapping_overage), allocation: 'unavailable', overage_unrecorded: workspace.overage_unrecorded === true, subscription_unrecorded_accounts: workspace.subscription_unrecorded_accounts === undefined ? 0 : count(workspace.subscription_unrecorded_accounts) };
	return {
		definition_version: 1,
		period: body.period === undefined ? null : decodePeriod(body.period),
		...(body.average_periods === undefined ? {} : { averagePeriods: decodeAveragePeriods(body.average_periods) }),
		series: body.series.map(period), latest: body.latest === null ? null : pooled(body.latest), previous: body.previous === null ? null : pooled(body.previous),
		coverage: { priced_records: count(coverage.priced_records), api_records: count(coverage.api_records) },
		not_api_priced_records: count(body.not_api_priced_records),
		excluded: body.excluded.map((item) => { const row = item as Record<string, unknown>; if (typeof row.kind !== 'string' || typeof row.text !== 'string') throw new Error('excluded cost records'); return { kind: row.kind, text: row.text, records: count(row.records) }; }),
		actual: { subscription_records: count(actual.subscription_records), unknown_charge_records: count(actual.unknown_charge_records), external_unset_records: actual.external_unset_records === undefined ? 0 : count(actual.external_unset_records), direct_api_dollars: actual.direct_api_dollars as number, complete: actual.complete as boolean, workspace_subscription: workspaceContext },
	};
}

export async function loadMeasurementCost(grain = get(measurementGrain), scope = get(measurementScope)): Promise<void> {
	controller?.abort();
	const next = new AbortController();
	controller = next;
	const key = scopeKey(scope);
	measurementCost.update((state) => ({ ...state, loading: true, error: null, data: state.scopeKey === key && state.grain === grain ? state.data : null }));
	try {
		const response = await fetch(`/api/measurement/cost?${grainScopeParams(grain, scope)}`, { signal: next.signal });
		if (!response.ok) throw new Error(`cost request failed: ${response.status}`);
		const data = decodeMeasurementCost(await response.json());
		if (controller !== next || key !== scopeKey(get(measurementScope))) return;
		measurementCost.set({ loading: false, error: null, data, scopeKey: key, grain });
	} catch (error) {
		if (controller !== next) return;
		console.warn('[measurement cost] load failed', error);
		measurementCost.update((state) => ({ ...state, loading: false, error: COST_LOAD_ERROR }));
	}
}
