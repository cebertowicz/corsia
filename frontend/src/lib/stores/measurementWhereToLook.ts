import { get, writable } from 'svelte/store';
import { measurementScope, panelSeriesIds, type MeasurementScope } from './measurement';
import { grainScopeParams, measurementGrain } from './measurementGrain';
import { decodePeriod, type MeasurementPeriod } from './measurementPeriod';

export type WhereToLookFormat = 'percent' | 'hours' | 'minutes' | 'ratio' | 'tokens' | 'dollars';
const FORMATS: ReadonlySet<unknown> = new Set(['percent', 'hours', 'minutes', 'ratio', 'tokens', 'dollars']);

export interface WhereToLookRow {
	subject: { id: string; name: string; kind: 'scope' | 'agent' | 'model' };
	measure: string;
	label: string;
	format: WhereToLookFormat;
	previous: number;
	latest: number;
	change: number;
	n: number;
	overall: number | null;
	action: string;
}

export interface WhereToLookState {
	loading: boolean;
	error: string | null;
	worse: WhereToLookRow[];
	improved: WhereToLookRow[];
	period: MeasurementPeriod | null;
	minimumN: number;
	scopeKey: string | null;
}

const LOAD_ERROR = 'Could not load the biggest changes.';
const key = (scope: MeasurementScope): string => `${scope.project}:${scope.agent ?? 'none'}`;
const finite = (value: unknown): number => {
	if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('where-to-look row carried an invalid number');
	return value;
};

function decodeRow(raw: unknown): WhereToLookRow {
	const row = raw as Record<string, unknown>;
	const subject = row.subject as Record<string, unknown> | undefined;
	if (!subject || typeof subject.id !== 'string' || typeof subject.name !== 'string') throw new Error('where-to-look row carried no subject');
	if (typeof row.measure !== 'string' || typeof row.label !== 'string' || typeof row.action !== 'string' || !FORMATS.has(row.format)) {
		throw new Error('where-to-look row carried an invalid measure');
	}
	return {
		subject: { id: subject.id, name: subject.name, kind: subject.kind === 'agent' || subject.kind === 'model' ? subject.kind : 'scope' },
		measure: row.measure, label: row.label, format: row.format as WhereToLookFormat, action: row.action,
		previous: finite(row.previous), latest: finite(row.latest), change: finite(row.change), n: finite(row.n),
		overall: typeof row.overall === 'number' && Number.isFinite(row.overall) ? row.overall : null,
	};
}

export function decodeWhereToLook(data: unknown): Pick<WhereToLookState, 'worse' | 'improved' | 'period' | 'minimumN'> {
	const payload = data as Record<string, unknown>;
	if (!Array.isArray(payload.worse) || !Array.isArray(payload.improved)) throw new Error('where-to-look response carried no lists');
	return {
		worse: payload.worse.map(decodeRow), improved: payload.improved.map(decodeRow),
		period: decodePeriod(payload.period),
		minimumN: finite(payload.minimum_n),
	};
}

const EMPTY = { worse: [], improved: [], period: null, minimumN: 20 } satisfies Pick<WhereToLookState, 'worse' | 'improved' | 'period' | 'minimumN'>;

export const measurementWhereToLook = writable<WhereToLookState>({ loading: true, error: null, ...EMPTY, scopeKey: null });
let controller: AbortController | null = null;

/** The moves compare the last window with the one before it, in the page grain. */
export async function loadMeasurementWhereToLook(scope = get(measurementScope), grain = get(measurementGrain)): Promise<void> {
	controller?.abort();
	const next = new AbortController();
	controller = next;
	const scopeKey = key(scope);
	measurementWhereToLook.update((state) => state.scopeKey === scopeKey
		? { ...state, loading: true, error: null }
		: { ...state, loading: true, error: null, ...EMPTY, scopeKey: null });
	try {
		const params = grainScopeParams(grain, scope);
		// The page's own series request, so the server answers both from one read.
		params.set('ids', panelSeriesIds());
		const response = await fetch(`/api/measurement/where-to-look?${params}`, { signal: next.signal });
		if (!response.ok) throw new Error(`where-to-look request failed: ${response.status}`);
		const decoded = decodeWhereToLook(await response.json());
		if (controller !== next || scopeKey !== key(get(measurementScope))) return;
		measurementWhereToLook.set({ loading: false, error: null, ...decoded, scopeKey });
	} catch (error) {
		if (controller !== next) return;
		console.warn('[where-to-look] load failed', error);
		measurementWhereToLook.update((state) => ({ ...state, loading: false, error: LOAD_ERROR }));
	}
}

/** A figure at a set number of decimals: a trailing zero drops at base precision, but stays once extra digits tell a comparison apart. */
function fixed(value: number, decimals: number, extra: number, grouping = false): string {
	const digits = Math.min(15, decimals);
	return Number(value.toFixed(digits)).toLocaleString('en-US', { useGrouping: grouping, minimumFractionDigits: extra > 0 ? digits : 0, maximumFractionDigits: digits });
}

function compact(value: number, extra = 0): string {
	const scaled = (divisor: number, unit: string) => `${fixed(value / divisor, (value / divisor < 10 ? 1 : 0) + extra, extra)}${unit}`;
	if (value >= 1e9) return scaled(1e9, 'B');
	if (value >= 1e6) return scaled(1e6, 'M');
	if (value >= 1e3) return scaled(1e3, 'k');
	return fixed(value, extra, extra);
}

/** One figure in its measure's unit, optionally showing extra digits to distinguish a comparison. */
export function formatWhereToLookValue(value: number, format: WhereToLookFormat, extra = 0): string {
	if (format === 'percent') return `${fixed(value * 100, extra, extra)}%`;
	if (format === 'tokens') return compact(value, extra);
	if (format === 'dollars') return `$${value.toFixed(Math.min(15, 2 + extra))}`;
	if (format === 'hours') return `${fixed(value, (value < 10 ? 1 : 0) + extra, extra, true)} h`;
	if (format === 'minutes') return value.toFixed(Math.min(15, 1 + extra));
	return value.toFixed(Math.min(15, 2 + extra));
}

/** Keep a reported change visible, applying the same precision to its comparison and overall. */
export function formatWhereToLookRow(row: Pick<WhereToLookRow, 'previous' | 'latest' | 'overall' | 'format'>): [string, string, string] {
	const values = [row.previous, row.latest, row.overall] as const;
	let extra = 0;
	let shown = values.map((value) => value === null ? '–' : formatWhereToLookValue(value, row.format, extra)) as [string, string, string];
	while (row.previous !== row.latest && shown[0] === shown[1] && extra < 15) {
		extra++;
		shown = values.map((value) => value === null ? '–' : formatWhereToLookValue(value, row.format, extra)) as [string, string, string];
	}
	return shown;
}

/** A relative change as a signed whole percent. */
export function formatChange(change: number): string {
	const percent = Math.round(change * 100);
	return `${percent > 0 ? '+' : ''}${percent}%`;
}
