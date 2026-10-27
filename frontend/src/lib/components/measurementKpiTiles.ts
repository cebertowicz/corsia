import { humanTokens } from '$lib/format';
import type { Kpi, KpiTokenFigure } from '$lib/stores/keyFigures';
import type { MeasurementCost } from '$lib/stores/measurementCost';
import type { PanelState, SeriesWindows } from '$lib/stores/measurement';
import { periodName } from '$lib/stores/measurementPeriod';
import { costView } from './measurementCostRate';
import { periodChange, periodView, sizeView } from './measurementPeriodView';
import { percentChange, versusPrevious } from './measurementSize';

/** One top-row tile: the label names the measure, the value is a number and its unit, the sub-line names both windows. */
export interface KpiTile {
	id: 'work' | 'first-try' | 'cost' | 'minutes' | 'tokens';
	label: string;
	value: string;
	sub: string;
	parts?: { id: string; label: string; value: string }[];
}

const money = (value: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value);

function windowLine(dates: string, n: number, change: string): string {
	return `${dates}, n\u00a0${n.toLocaleString('en-US')}, ${change}`;
}

/** The top row's windowed measures: the section panel each one reads and its series, so a tile is the figure its headline shows. */
export const KPI_MEASURES = {
	work: { panel: 'size', series: 'estimated_human_hours_per_active_hour' },
	'first-try': { panel: 'first-try', series: 'first_try_acceptance' },
	minutes: { panel: 'attention', series: 'active_hours_per_outcome' },
} as const;

/**
 * The figure, n and change a tile prints are the ones its section headline prints, from the same view and rule: the served pooled
 * window, and a previous window the measure covers only in part reads new.
 */
function headlineFigure(id: keyof typeof KPI_MEASURES, panel: PanelState, window: SeriesWindows) {
	const view = id === 'work' ? sizeView(panel.rows, panel.period, window) : periodView(panel.rows, KPI_MEASURES[id].series, 1, panel.period, window);
	return { value: view.current.value, n: view.current.n, change: periodChange(view, true), previousN: view.previous?.n ?? 0, dates: view.nowText };
}

const emptyText = {
	work: 'Shows when work and your attention are recorded.',
	'first-try': 'Shows when agents grade accepted outcomes.',
	minutes: 'Shows when your attention and accepted outcomes are recorded.',
} as const;

function seriesTile(id: keyof typeof KPI_MEASURES, label: string, panel: PanelState | undefined, value: (v: number) => string): KpiTile | null {
	const window: SeriesWindows | undefined = panel?.windows?.[KPI_MEASURES[id].series];
	if (!panel?.period || !window) return null;
	const figure = headlineFigure(id, panel, window);
	const dates = figure.dates || periodName(panel.period.latest);
	return { id, label, value: figure.value === null ? '–' : value(figure.value), sub: figure.n === 0 && figure.previousN === 0 ? emptyText[id] : windowLine(dates, figure.n, figure.change) };
}

/** The four measures the sections below headline, from the panels' own windows blocks and the cost payload. */
export function measureTiles(panels: Record<string, PanelState>, cost: MeasurementCost | null): Partial<Record<KpiTile['id'], KpiTile>> {
	const tiles: Partial<Record<KpiTile['id'], KpiTile>> = {};
	const size = seriesTile('work', 'Work per hour of your attention', panels[KPI_MEASURES.work.panel], (v) => `${v.toFixed(0)} h`);
	const firstTry = seriesTile('first-try', 'First-try rate (agent-graded)', panels[KPI_MEASURES['first-try'].panel], (v) => `${(v * 100).toFixed(0)}%`);
	const minutes = seriesTile('minutes', 'Your minutes per outcome', panels[KPI_MEASURES.minutes.panel], (v) => `${(v * 60).toFixed(1)} min`);
	if (size) tiles.work = size;
	if (firstTry) tiles['first-try'] = firstTry;
	if (minutes) tiles.minutes = minutes;
	const costPeriod = cost?.period ?? panels[KPI_MEASURES.work.panel]?.period;
	if (cost && costPeriod) {
		const view = costView({ ...cost, period: costPeriod });
		const rate = view ? view.current.perHour : null;
		const dates = periodName(costPeriod.latest);
		tiles.cost = { id: 'cost', label: 'Cost per person-hour of work delivered', value: rate === null ? '–' : money(rate), sub: !view || (view.current.sized === 0 && (view.previous?.sized ?? 0) === 0) ? 'Shows when work and its cost are recorded.' : windowLine(dates, view.current.sized, view.change) };
	}
	return tiles;
}

/** A token figure as read, to three significant digits on every figure of the tile: `1.29M`, `14.9M`, and `1.2M+` when some calls recorded none of it, so the sum is a lower bound. */
function tokens(figure: KpiTokenFigure): string {
	return `${humanTokens(figure.value, 3)}${figure.unknown_records ? '+' : ''}`;
}

/** The dates a tokens window covers, as the key figures serve them. */
const span = (window: { from: string; to: string }): string => periodName(window);

/** The total over the latest window beside the previous one, with the four parts that add up to it. */
export function tokensTile(kpi: Kpi): KpiTile {
	const total = kpi.current.tokens.total, before = kpi.previous.tokens.total;
	const change = before.value > 0 && kpi.previous.tokens_recorded !== false ? percentChange(total.value, before.value) : 'new';
	const parts = kpi.current.tokens;
	const empty = total.value === 0 && before.value === 0;
	return {
		id: 'tokens', label: 'Tokens', value: empty ? '–' : tokens(total),
		sub: empty ? 'Shows when agents use tokens.' : change === 'new' ? span(kpi.current) : `${span(kpi.current)}, ${versusPrevious(change, span(kpi.previous))}`,
		parts: empty ? undefined : [
			{ id: 'input', label: 'Uncached input', value: tokens(parts.input) },
			{ id: 'cache-write', label: 'Cache write', value: tokens(parts.cache_write) },
			{ id: 'cache-read', label: 'Cache read', value: tokens(parts.cache_read) },
			{ id: 'output', label: 'Output', value: tokens(parts.output) },
		],
	};
}
