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

const group = (value: number) => value.toLocaleString('en-US');
const money = (value: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value);

/** "Sep 28 to Oct 4, n 1,349, +206% vs Sep 21 to Sep 27 (n 451)": the dates behind the figure, its n, and the change the section headline below reads for the same measure. */
export function windowLine(dates: string, latest: { n: number }, change: string): string {
	return `${dates}, n ${group(latest.n)}, ${change}`;
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
function headlineFigure(id: keyof typeof KPI_MEASURES, panel: PanelState, window: SeriesWindows): { value: number | null; n: number; change: string; dates: string } {
	const view = id === 'work' ? sizeView(panel.rows, panel.period, window) : periodView(panel.rows, KPI_MEASURES[id].series, 1, panel.period, window);
	return { value: view.current.value, n: view.current.n, change: periodChange(view, true), dates: view.nowText };
}

function seriesTile(id: keyof typeof KPI_MEASURES, label: string, panel: PanelState | undefined, value: (v: number) => string): KpiTile | null {
	const window: SeriesWindows | undefined = panel?.windows?.[KPI_MEASURES[id].series];
	if (!panel?.period || !window) return null;
	const figure = headlineFigure(id, panel, window);
	return { id, label, value: figure.value === null ? '–' : value(figure.value), sub: windowLine(figure.dates || periodName(panel.period.latest), figure, figure.change) };
}

/** The four measures the sections below headline, from the panels' own windows blocks and the cost payload. */
export function measureTiles(panels: Record<string, PanelState>, cost: MeasurementCost | null): Partial<Record<KpiTile['id'], KpiTile>> {
	const tiles: Partial<Record<KpiTile['id'], KpiTile>> = {};
	const size = seriesTile('work', 'Hours of work per hour of your attention', panels[KPI_MEASURES.work.panel], (v) => `${v.toFixed(0)} h`);
	const firstTry = seriesTile('first-try', 'First-try rate (agent-graded)', panels[KPI_MEASURES['first-try'].panel], (v) => `${(v * 100).toFixed(0)}%`);
	const minutes = seriesTile('minutes', 'Your minutes per outcome', panels[KPI_MEASURES.minutes.panel], (v) => `${(v * 60).toFixed(1)} min`);
	if (size) tiles.work = size;
	if (firstTry) tiles['first-try'] = firstTry;
	if (minutes) tiles.minutes = minutes;
	const costPeriod = cost?.period ?? panels[KPI_MEASURES.work.panel]?.period;
	if (cost && costPeriod) {
		const view = costView({ ...cost, period: costPeriod });
		const rate = view ? view.current.perHour : null;
		tiles.cost = { id: 'cost', label: 'Cost per hour of delivered work', value: rate === null ? '–' : money(rate), sub: windowLine(periodName(costPeriod.latest), { n: view?.current.sized ?? 0 }, view?.change ?? 'n/a') };
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
	const change = before.value > 0 ? percentChange(total.value, before.value) : 'new';
	const parts = kpi.current.tokens;
	return {
		id: 'tokens', label: 'Tokens', value: tokens(total),
		sub: `${span(kpi.current)}, ${versusPrevious(change, span(kpi.previous))}`,
		parts: [
			{ id: 'input', label: 'Uncached input', value: tokens(parts.input) },
			{ id: 'cache-write', label: 'Cache write', value: tokens(parts.cache_write) },
			{ id: 'cache-read', label: 'Cache read', value: tokens(parts.cache_read) },
			{ id: 'output', label: 'Output', value: tokens(parts.output) },
		],
	};
}
