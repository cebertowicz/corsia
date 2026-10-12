import { get, writable } from 'svelte/store';
import { measurementScope, type MeasurementScope } from './measurement';
import { decodePeriod, type MeasurementPeriod } from './measurementPeriod';
import { grainScopeParams, measurementGrain, type MeasurementGrain } from './measurementGrain';

export type ModelCostClass = 'priced' | 'local' | 'no_pricing';
const COST_CLASSES: ReadonlySet<unknown> = new Set(['priced', 'local', 'no_pricing']);
/** Why the rate cell has no figure; `none` means it has one. */
export type ModelRateReason = 'none' | 'no_tasks' | 'unpriced' | 'local';
const RATE_REASONS: ReadonlySet<unknown> = new Set(['none', 'no_tasks', 'unpriced', 'local']);
/** Why the change cell has no percentage; `none` with no change is a $0 previous rate or a week with no tasks. */
export type ModelChangeReason = 'none' | 'new' | 'too_few_before' | 'unpriced' | 'local';
const CHANGE_REASONS: ReadonlySet<unknown> = new Set(['none', 'new', 'too_few_before', 'unpriced', 'local']);

/** Size classes in their order; a row carries a cell only for classes with enough tasks. */
export const MODEL_SIZE_CLASSES = ['XS', 'S', 'M', 'L', 'XL', 'XXL'] as const;
export type ModelSizeClass = (typeof MODEL_SIZE_CLASSES)[number];
const SIZE_CLASSES: ReadonlySet<unknown> = new Set(MODEL_SIZE_CLASSES);

/** The row's $ per accepted task restricted to one size class, with its change and first-try share. */
export interface ModelSizeCell {
	size: ModelSizeClass;
	tasks: number;
	cost_per_task: number;
	cost_change: number | null;
	/** `new` when the previous week had too few of this class for a percentage. */
	change_reason: 'none' | 'new';
	/** Share of the class's tasks accepted without being sent back, as graded by the agents. */
	first_try: number;
	/** Some calls had no price, so the figure is a lower bound. */
	partial: boolean;
	recording_gap: boolean;
}

export interface MeasurementModelRow {
	/** Full model identity; empty for the Other row. */
	model: string;
	label: string;
	tasks: number;
	failed_turns_per_100: number | null;
	recorded_cost_per_task: number | null;
	/** Ledger priced API-equivalent dollars of every call in the latest window; null when nothing was priced, never 0. */
	spend_usd: number | null;
	cost_class: ModelCostClass | null;
	/** The rate excludes local calls, which have no list price, and the group includes some. */
	includes_local: boolean;
	/** A task's call failed to reach the ledger, so the rate may be low. */
	recording_gap: boolean;
	rate_reason: ModelRateReason;
	cost_change: number | null;
	change_reason: ModelChangeReason;
	previous_tasks: number;
	not_offered_now: boolean;
	other: boolean;
	sizes: ModelSizeCell[];
}

export interface ModelTokenShare {
	model: string;
	label: string;
	share_of_tokens: number;
	/** The slice's token total in the same window; the remainder slice sums its models. */
	tokens: number;
}

export interface ModelsState {
	loading: boolean;
	error: string | null;
	rows: MeasurementModelRow[];
	/** Accepted tasks a model needs for its own table row, as the backend groups them. */
	minTasks: number | null;
	tokenShares: ModelTokenShare[];
	unmatchedFailedTurns: number;
	unresolvedTokenShare: number;
	/** Ledger priced dollars of every call in the latest window: what each row's `spend_usd` is a share of. */
	totalSpendUsd: number;
	/** The windows the response answered for; null when it served none. */
	period: MeasurementPeriod | null;
	scopeKey: string | null;
	grain: MeasurementGrain | null;
}

function modelsScopeKey(scope: MeasurementScope): string { return `${scope.project}:${scope.agent ?? 'none'}`; }
const MODELS_LOAD_ERROR = 'Could not load models.';
const number = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null;
const integer = (value: unknown): number => {
	if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error('models response carried invalid count');
	return value as number;
};

const spendTotal = (value: unknown): number => {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new Error('models response carried invalid spend total');
	return value;
};

const share = (value: unknown): number => {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) throw new Error('models response carried invalid unresolved token share');
	return value;
};

function decodeSizeCell(raw: unknown): ModelSizeCell {
	const cell = raw as Record<string, unknown>;
	const cost = number(cell.cost_per_task);
	const firstTry = number(cell.first_try);
	if (
		!SIZE_CLASSES.has(cell.size) || cost === null || cost < 0 || firstTry === null || firstTry < 0 || firstTry > 1
		|| (cell.change_reason !== 'none' && cell.change_reason !== 'new')
		|| (cell.cost_change !== null && number(cell.cost_change) === null)
	) {
		throw new Error('models row carried invalid size cell');
	}
	return {
		size: cell.size as ModelSizeClass,
		tasks: integer(cell.tasks),
		cost_per_task: cost,
		cost_change: number(cell.cost_change),
		change_reason: cell.change_reason,
		first_try: firstTry,
		partial: cell.partial === true,
		recording_gap: cell.recording_gap === true,
	};
}

type Decoded = Pick<ModelsState, 'rows' | 'minTasks' | 'tokenShares' | 'unmatchedFailedTurns' | 'unresolvedTokenShare' | 'totalSpendUsd' | 'period'>;

function decodeRow(raw: unknown): MeasurementModelRow {
	const row = raw as Record<string, unknown>;
	if (typeof row.model !== 'string' || typeof row.label !== 'string' || typeof row.not_offered_now !== 'boolean') {
		throw new Error('models row carried invalid model');
	}
	if (row.cost_class !== null && !COST_CLASSES.has(row.cost_class)) throw new Error('models row carried invalid cost class');
	if (!RATE_REASONS.has(row.rate_reason) || !CHANGE_REASONS.has(row.change_reason)) throw new Error('models row carried invalid cost reason');
	if (row.sizes !== undefined && !Array.isArray(row.sizes)) throw new Error('models row carried invalid size cells');
	return {
		model: row.model,
		label: row.label,
		tasks: integer(row.tasks),
		failed_turns_per_100: number(row.failed_turns_per_100),
		recorded_cost_per_task: number(row.recorded_cost_per_task),
		spend_usd: number(row.spend_usd),
		cost_class: row.cost_class as ModelCostClass | null,
		includes_local: row.includes_local === true,
		recording_gap: row.recording_gap === true,
		rate_reason: row.rate_reason as ModelRateReason,
		cost_change: number(row.cost_change),
		change_reason: row.change_reason as ModelChangeReason,
		previous_tasks: integer(row.previous_tasks),
		not_offered_now: row.not_offered_now,
		other: row.other === true,
		sizes: ((row.sizes as unknown[] | undefined) ?? []).map(decodeSizeCell),
	};
}

function decodeShare(raw: unknown): ModelTokenShare {
	const row = raw as Record<string, unknown>;
	if (
		typeof row.model !== 'string' || typeof row.label !== 'string'
		|| typeof row.share_of_tokens !== 'number' || row.share_of_tokens < 0
	) {
		throw new Error('models response carried invalid token share');
	}
	return { model: row.model, label: row.label, share_of_tokens: row.share_of_tokens, tokens: integer(row.tokens) };
}

export function decodeModelsResponse(data: unknown): Decoded {
	const payload = data as Record<string, unknown>;
	if (!Array.isArray(payload.rows) || !Array.isArray(payload.token_shares)) throw new Error('models response carried no rows');
	return {
		rows: payload.rows.map(decodeRow),
		minTasks: integer(payload.min_tasks),
		tokenShares: payload.token_shares.map(decodeShare),
		unmatchedFailedTurns: integer(payload.unmatched_failed_turns),
		unresolvedTokenShare: share(payload.unresolved_token_share),
		totalSpendUsd: spendTotal(payload.total_spend_usd),
		period: payload.period === undefined ? null : decodePeriod(payload.period),
	};
}

const EMPTY = { rows: [], minTasks: null, tokenShares: [], unmatchedFailedTurns: 0, unresolvedTokenShare: 0, totalSpendUsd: 0, period: null } satisfies Decoded;

export const measurementModels = writable<ModelsState>({
	loading: true, error: null, ...EMPTY, scopeKey: null, grain: null,
});
let controller: AbortController | null = null;

export async function loadMeasurementModels(grain = get(measurementGrain), scope = get(measurementScope)): Promise<void> {
	controller?.abort();
	const next = new AbortController();
	controller = next;
	const scopeKey = modelsScopeKey(scope);
	measurementModels.update((state) => {
		// Keep the shown result while the same scope and grain refresh; clear it otherwise.
		if (state.scopeKey === scopeKey && state.grain === grain) return { ...state, loading: true, error: null };
		return { ...state, loading: true, error: null, ...EMPTY, scopeKey: null, grain: null };
	});
	try {
		const response = await fetch(`/api/measurement/models?${grainScopeParams(grain, scope)}`, { signal: next.signal });
		if (!response.ok) throw new Error(`models request failed: ${response.status}`);
		const decoded = decodeModelsResponse(await response.json());
		if (controller !== next || scopeKey !== modelsScopeKey(get(measurementScope))) return;
		measurementModels.set({ loading: false, error: null, ...decoded, scopeKey, grain });
	} catch (error) {
		if (controller !== next) return;
		console.warn('[models] load failed', error);
		measurementModels.update((state) => ({ ...state, loading: false, error: MODELS_LOAD_ERROR }));
	}
}
