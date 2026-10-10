import { get, writable } from 'svelte/store';
import { token } from './auth';
import { MEASUREMENT_VIEW, clearMeasurementUrl } from './measurementAddress';
import { grainScopeParams, measurementGrain, parseGrain, type MeasurementGrain } from './measurementGrain';
import { decodeAveragePeriods, decodePeriod, type MeasurementPeriod } from './measurementPeriod';
import { decodeSeriesRows } from './measurementRows';

export type MeasurementScope = { project: string; agent: string | null };

export interface MeasurementRow {
	period: string;
	series: string;
	value: number | null;
	n: number;
	proxy: boolean;
	partial: boolean;
	comparable: boolean;
	break: { kind: string; reason: string } | null;
	dimension: string | null;
	thin: boolean;
	/** Coverage and provenance, served only on the estimated-size series. */
	size?: SizeCoverage;
	/** Records behind this value that carried no count, so the value is a lower bound. Daily rows and the token total carry it. */
	unknown?: number;
	/** The token total only: cache re-reads in the same calls, which the value leaves out. */
	cached?: number;
	/** The moving average at this period, on the value's own axis: pooled over the grain's 7 days or 4 weeks ending here. Null before the series has that many periods. */
	average?: number | null;
	/** The value before the minimum-n withholding: present on a low-n period whose `value` is null. A chart draws it as a hollow bar; it is never a headline. */
	raw_value?: number | null;
}

/** One period's size estimate behind its rate: what was sized, what was not, and by whom. */
export interface SizeCoverage {
	outcomes: number;
	sized: number;
	estimated_hours: number;
	active_hours: number;
	unsized: number;
	unknown: number;
	stale: number;
	conflict: number;
	retracted: number;
	malformed: number;
	basis: { delivered_evidence_proxy: number; accepted_scope_proxy: number; author_declared: number };
	profiles: { method: string; method_version: string; rubric_version: string; rater: string; rater_version: string; outcomes: number }[];
	comparable_reason: string | null;
	rate_reason: string | null;
	class_midpoint_hours: Record<string, number>;
	/** The registry's data start and comparability break (ISO dates), served with the row. */
	data_start: string;
	comparability_before: string;
}

/** How one measurement value is rendered. */
export type MeasurementFormat = 'count' | 'hours' | 'minutes' | 'percent' | 'ratio';

const countFormat = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const oneDecimalFormat = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });

/** One decimal with thousands grouped; a non-zero value that would blank keeps two significant digits. */
function formatOneDecimal(value: number): string {
	if (value !== 0 && Math.abs(value) < 0.05) return String(Number(value.toPrecision(2)));
	return oneDecimalFormat.format(value);
}

/** Render one ratio without distorting the value the API served.
 *
 * At most two decimals keeps a ratio readable and never rounds away a digit the
 * integer part carries: 762 stays 762. Below 0.01 a fixed two decimals would
 * blank a measured value to zero, so those keep two significant digits.
 */
function formatRatio(value: number): string {
	if (value !== 0 && Math.abs(value) < 0.01) return String(Number(value.toPrecision(2)));
	return String(Number(value.toFixed(2)));
}

/** Render one measurement value for the unit shown on the surface. */
export function formatMeasurementValue(value: number | null, format: MeasurementFormat = 'ratio'): string {
	if (value === null || Number.isNaN(value)) return '';
	if (format === 'percent') return `${Math.round(value * 100)}%`;
	if (format === 'count') return countFormat.format(value);
	if (format === 'hours' || format === 'minutes') return formatOneDecimal(value);
	return formatRatio(value);
}

export interface PanelState {
	loading: boolean;
	rows: MeasurementRow[];
	error: string | null;
	/** The grain and scope this state describes; its rows were loaded for exactly that. */
	selection: string;
	/** The periods the response answered for; a headline pools the rows these windows name, never a count of its own. */
	period?: MeasurementPeriod;
	/** The backend's pooled latest and previous figure per series id, the value every headline must equal. */
	windows?: Record<string, SeriesWindows>;
	/** How many periods each served moving average spans; the chart names its line by it. */
	averagePeriods?: number;
	/** The served minimum n per series id: under it a period is withheld and a change reads low n. */
	minimumN?: Record<string, number>;
}

/** The served minimum n per series; a malformed entry rejects the response, never defaults. */
function decodeMinimumN(value: unknown): Record<string, number> | undefined {
	if (value === undefined) return undefined;
	if (!value || typeof value !== 'object') throw new Error('minimum_n shape');
	return Object.fromEntries(Object.entries(value).map(([id, n]) => {
		if (typeof n !== 'number' || !Number.isInteger(n) || n < 0) throw new Error('minimum_n shape');
		return [id, n];
	}));
}

/** The counts the server pooled behind a size window's rate: the window's own coverage, never re-pooled from rows. */
export type WindowSize = { outcomes: number; sized: number; excluded: number; estimated_hours: number; active_hours: number };
/** `from` and `to` are the dates the server pooled, served when it pooled any: narrower than the window when a series starts or breaks inside it. */
export type WindowFigure = { value: number | null; n: number; size?: WindowSize; from?: string; to?: string };
export type SeriesWindows = { latest: WindowFigure; previous: WindowFigure };

export const measurementScope = writable<MeasurementScope>({ project: 'all', agent: null });
export const measurementPanels = writable<Record<string, PanelState>>({});
const controllers = new Map<string, AbortController>();

export const panelSeries: Record<string, string[]> = {
	outcomes: ['outcomes'],
	attention: ['active_hours_per_outcome'],
	asks: ['asks_per_outcome'],
	alignment: ['took_recommendation_pct', 'answered_other_pct', 'answered_neither_pct'],
	'first-try': ['first_try_acceptance'],
	rejections: ['rejections_per_outcome'],
	tokens: ['fresh_tokens_per_outcome'],
	size: ['estimated_human_hours_per_active_hour'],
};

/** The series ids of one request for the given panels, in the order the server answers them. */
export function panelSeriesIds(panels: string[] = Object.keys(panelSeries)): string {
	return panels.flatMap((panel) => panelSeries[panel]).join(',');
}

function selectionKey(grain: MeasurementGrain, scope: MeasurementScope): string {
	return `${grain}:${scope.project}:${scope.agent ?? 'none'}`;
}

/**
 * The one writer of the composite scope.
 *
 * Project and agent always move as one pair, and no surface keeps a private copy
 * of the selection: a row action that filters to one agent sets both at once, and
 * a project change drops an agent that is not in it rather than leaving a filter
 * that matches nothing. Everything that moves the scope arrives through this
 * function -- setMeasurementProject and filterMeasurementAgent above, and
 * the doorway helpers in measurementNavigation.ts (openMeasurementFromRail,
 * openMeasurementFromThread, openMeasurementFromProject), which are entries
 * to the same state rather than a second copy of it.
 */
export function writeMeasurementScope(
	next: MeasurementScope,
	agentProject?: (agent: string) => string | null | undefined,
): void {
	const agent = next.agent && (next.project === 'all' || !agentProject || agentProject(next.agent) === next.project)
		? next.agent
		: null;
	measurementScope.set({ project: next.project, agent });
}

/** Changing project drops an agent that is not in the newly selected project; every agent is in all work. */
export function setMeasurementProject(project: string | 'all', agentProjectById: Map<string, string | null>): void {
	const current = get(measurementScope);
	writeMeasurementScope({ project, agent: current.agent }, (agent) => agentProjectById.get(agent));
}

export function clearMeasurementAgent(): void {
	const current = get(measurementScope);
	writeMeasurementScope({ project: current.project, agent: null });
}

/** Filter to one agent: the project and the agent come from the same row and land together. */
export function filterMeasurementAgent(agent: string, project: string | null): void {
	writeMeasurementScope({ project: project ?? 'all', agent });
}

let applyingUrl = false;
let urlSyncStarted = false;

/**
 * The selection lives in the URL as well as in these stores.
 *
 * A doorway writes its scope before this surface mounts so the page hydrates to
 * what the reader clicked, a reload returns to the same view, and back and forward
 * walk the selections already made (PL-2).
 */
function syncUrl(): void {
	if (applyingUrl || typeof window === 'undefined') return;
	const scope = get(measurementScope);
	// Every other parameter the app itself owns survives this write: Measurement adds
	// the three keys it answers for and never takes a thread or a document away.
	const params = new URLSearchParams(window.location.search);
	params.set('view', MEASUREMENT_VIEW);
	params.set('project', scope.project);
	params.set('agent', scope.agent ?? 'none');
	params.set('grain', get(measurementGrain));
	params.delete('range');
	const query = params.toString();
	window.history.replaceState(window.history.state, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
}

/** Makes a doorway scope durable before Measurement mounts and hydrates from the URL. */
export function syncMeasurementUrl(): void {
	syncUrl();
}

function hydrateUrl(): void {
	if (typeof window === 'undefined') return;
	const params = new URLSearchParams(window.location.search);
	const project = params.get('project');
	const agent = params.get('agent');
	applyingUrl = true;
	measurementGrain.set(parseGrain(params.get('grain')));
	measurementScope.set({
		project: project && project !== 'none' ? project : 'all',
		agent: agent && agent !== 'none' ? agent : null,
	});
	applyingUrl = false;
	syncUrl();
}

/** Reads the URL once on entry, then keeps it and the selection in step. */
export function startMeasurementUrlSync(): () => void {
	if (typeof window === 'undefined' || urlSyncStarted) return () => {};
	urlSyncStarted = true;
	hydrateUrl();
	const unsubscribeGrain = measurementGrain.subscribe(syncUrl);
	const unsubscribeScope = measurementScope.subscribe(syncUrl);
	const onPopState = () => hydrateUrl();
	window.addEventListener('popstate', onPopState);
	return () => { unsubscribeGrain(); unsubscribeScope(); window.removeEventListener('popstate', onPopState); urlSyncStarted = false; clearMeasurementUrl(); };
}

/** Stop a panel's request; a request other panels still share keeps running for them. */
function releasePanel(panel: string): void {
	const controller = controllers.get(panel);
	controllers.delete(panel);
	if (controller && ![...controllers.values()].includes(controller)) controller.abort();
}

/**
 * Load several panels with one series request.
 *
 * Each panel keeps its own state: rows are split by series, and a panel that a
 * newer load or a retry has taken over since is left to that load.
 */
async function loadPanels(panels: string[], grain: MeasurementGrain, scope: MeasurementScope): Promise<void> {
	for (const panel of panels) releasePanel(panel);
	const controller = new AbortController();
	for (const panel of panels) controllers.set(panel, controller);
	const owned = () => panels.filter((panel) => controllers.get(panel) === controller);
	const selection = selectionKey(grain, scope);
	// Rows stay on screen through a reload or a retry of the selection that
	// loaded them. A different selection starts empty, so one project's values
	// are never shown under another while it loads or after it fails.
	const settle = (panelState: (panel: string, states: Record<string, PanelState>) => PanelState) =>
		measurementPanels.update((states) => ({ ...states, ...Object.fromEntries(owned().map((panel) => [panel, panelState(panel, states)])) }));
	const keptRows = (panel: string, states: Record<string, PanelState>) => states[panel]?.selection === selection ? states[panel].rows : [];
	// The kept rows keep the periods they were answered for, so a headline never pools them under a window they did not come from.
	const keptWindows = (panel: string, states: Record<string, PanelState>): Partial<PanelState> => {
		const kept = states[panel]?.selection === selection ? states[panel] : undefined;
		return { ...(kept?.period ? { period: kept.period } : {}), ...(kept?.windows ? { windows: kept.windows } : {}),
			...(kept?.averagePeriods ? { averagePeriods: kept.averagePeriods } : {}), ...(kept?.minimumN ? { minimumN: kept.minimumN } : {}) };
	};
	settle((panel, states) => ({ loading: true, rows: keptRows(panel, states), error: null, selection, ...keptWindows(panel, states) }));
	try {
		const params = grainScopeParams(grain, scope);
		params.set('ids', panelSeriesIds(panels));
		const response = await fetch(`/api/measurement/series?${params}`, { signal: controller.signal });
		if (!response.ok) throw new Error('Could not load measurement');
		const data = await response.json() as { rows?: MeasurementRow[]; period?: unknown; windows?: Record<string, SeriesWindows>; average_periods?: unknown; minimum_n?: unknown };
		if (selection !== selectionKey(get(measurementGrain), get(measurementScope))) return;
		const rows = decodeSeriesRows(data.rows ?? []);
		const period = data.period === undefined ? undefined : decodePeriod(data.period);
		const averagePeriods = decodeAveragePeriods(data.average_periods), minimumN = decodeMinimumN(data.minimum_n);
		settle((panel) => ({
			loading: false, rows: rows.filter((row) => panelSeries[panel].includes(row.series)), error: null, selection,
			...(period ? { period } : {}), ...(averagePeriods ? { averagePeriods } : {}), ...(minimumN ? { minimumN } : {}),
			...(data.windows ? { windows: Object.fromEntries(panelSeries[panel].filter((id) => data.windows![id]).map((id) => [id, data.windows![id]])) } : {}),
		}));
	} catch (error) {
		if ((error as DOMException).name === 'AbortError') return;
		settle((panel, states) => ({ loading: false, rows: keptRows(panel, states), error: 'Could not load this measurement.', selection, ...keptWindows(panel, states) }));
	}
}

/** Load or retry one panel on its own. */
export async function loadMeasurementPanel(panel: string, grain = get(measurementGrain), scope = get(measurementScope)): Promise<void> {
	return loadPanels([panel], grain, scope);
}

/** Load every panel with one series request, so a page load is one bulk-lane job rather than one per panel. */
export async function loadMeasurementPanels(grain = get(measurementGrain), scope = get(measurementScope)): Promise<void> {
	return loadPanels(Object.keys(panelSeries), grain, scope);
}
