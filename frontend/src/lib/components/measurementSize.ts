import { type MeasurementRow, type SizeCoverage, type WindowSize } from '$lib/stores/measurement';
import { drawnValue } from '$lib/stores/measurementRows';
import type { BarPoint } from './measurementBarChart';
import { periodName, type MeasurementPeriod, type PeriodSpan } from '$lib/stores/measurementPeriod';

/**
 * Product minimum n for a change where no minimum is served with it (the size and cost rates, the KPI tiles). A series the
 * `/series` response serves its own `minimum_n` for reads that instead; the two can differ, so this is not the server's figure.
 */
export const MIN_N = 20;
export const SIZE_SERIES = 'estimated_human_hours_per_active_hour';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAY_MS = 86_400_000;

/** "Sep 28": a calendar date as written in the render, with no year. */
export function shortDate(iso: string): string {
	return `${MONTHS[Number(iso.slice(5, 7)) - 1]} ${Number(iso.slice(8, 10))}`;
}
export function addDays(iso: string, days: number): string {
	return new Date(Date.parse(`${iso.slice(0, 10)}T00:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10);
}
/** The first and last day a run of weeks covers, e.g. "Sep 7 to Oct 4". */
export function spanText(periodStarts: string[]): string {
	return `${shortDate(periodStarts[0])} to ${shortDate(addDays(periodStarts[periodStarts.length - 1], 6))}`;
}

/** Whether a period (its first local date) falls inside a served window, both ends inclusive. */
export const inSpan = (period: string, span: PeriodSpan): boolean => period >= span.from && period <= span.to;

/** How many periods of the response's grain a served window holds: 7 days, or 4 weeks. */
export function spanPeriods(period: MeasurementPeriod, span: PeriodSpan): number {
	const days = Math.round((Date.parse(span.to) - Date.parse(span.from)) / DAY_MS) + 1;
	return period.grain === 'days' ? days : Math.round(days / 7);
}

/** The weeks every Weeks chart draws: the served previous and latest comparison windows, eight completed weeks, oldest first, and any later week the series serves. */
function weekAxis(period: MeasurementPeriod, points: BarPoint[]): string[] {
	const end = points.reduce((last, point) => (point.start > last ? point.start : last), period.latest.to);
	const weeks: string[] = [];
	for (let week = period.previous.from; week <= end; week = addDays(week, 7)) weeks.push(week);
	return weeks;
}

/**
 * Each chart's drawn points, the dates they span, what one bar is and the line drawn over it. On Weeks every chart shares one axis,
 * the weeks of `weekAxis`: a week the series has no point for (its measure had not started) is drawn empty, and weeks read before
 * the axis only feed the averages. Days draw the series as served.
 */
export function grainChart(period: MeasurementPeriod | undefined, points: BarPoint[], averagePeriods: number | undefined): { unit: 'day' | 'week'; averageName: string; points: BarPoint[]; rangeText: string } {
	const unit = period?.grain === 'days' ? 'day' : 'week';
	const averageName = averagePeriods ? `${averagePeriods}-${unit} moving average` : 'moving average';
	const byStart = new Map(points.map((point) => [point.start, point]));
	const drawn = period?.grain === 'weeks' && points.length
		? weekAxis(period, points).map((start): BarPoint => byStart.get(start) ?? { start, value: null, n: 0, ma: null, empty: true })
		: points;
	return {
		unit, averageName, points: drawn,
		rangeText: period && drawn.length ? `${shortDate(drawn[0].start)} to ${shortDate(period.range.to)}` : '',
	};
}

export interface PeriodSize { period: string; value: number | null; size: SizeCoverage; row: MeasurementRow }

/**
 * Weeks from the first recorded week on. A week before it is not drawn; an unknown week after it stays unknown. The chart draws
 * these; the headline window is pooled by the server over the whole window the card names, comparable or not.
 */
export function sizePeriods(rows: MeasurementRow[]): PeriodSize[] {
	const all = rows.filter((row) => row.series === SIZE_SERIES && row.size)
		.map((row) => ({ period: row.period, value: drawnValue(row), size: row.size as SizeCoverage, row }))
		.sort((a, b) => a.period.localeCompare(b.period));
	// Weeks before a served comparability break are counted differently, so the series starts after the last one.
	// When the latest week itself is not comparable it stands alone: its rate is still served, but there is nothing to compare or draw.
	all.splice(0, Math.min(all.length - 1, all.reduce((at, w, i) => (w.row.comparable ? at : i + 1), 0)));
	const first = all.findIndex((w) => w.value !== null);
	return first < 0 ? [] : all.slice(first);
}

/** Hours of work per size-rated outcome, and your minutes per one, from a served window's pooled counts: undefined, never zero, when nothing was sized or no active time was recorded. */
export const hoursPerOutcome = (s: WindowSize | undefined): number | null => (s && s.sized > 0 ? s.estimated_hours / s.sized : null);
export const minutesPerOutcome = (s: WindowSize | undefined): number | null => (s && s.sized > 0 && s.active_hours > 0 ? (s.active_hours * 60) / s.sized : null);

/** One bar per week, and the moving average the server served for it; a week without one draws no point on the line. */
export function barPoints(periods: PeriodSize[]): BarPoint[] {
	return periods.map((w) => ({ start: w.period, value: w.value, n: w.size.sized, ma: w.row.average ?? null }));
}

export function percentChange(cur: number, prev: number): string {
	const c = (100 * (cur - prev)) / prev;
	if (Math.abs(c) < 0.5) return '0%';
	return `${Math.round(c) >= 0 ? '+' : ''}${c.toFixed(0)}%`;
}

/** The one change rule: under the minimum n now reads low n; under it before, no previous window, or a measure recorded only part of the previous window reads new. */
export function changeText(cur: number | null, prev: number | null, nCur: number, nPrev: number, sourceNew = false, minimum = MIN_N): string {
	if (cur === null) return 'n/a';
	if (nCur < minimum) return 'low n';
	if (prev === null || prev === 0 || nPrev < minimum || sourceNew) return 'new';
	return percentChange(cur, prev);
}

/** "-4% vs Sep 21 to Sep 27 (n 451)": a comparison against the previous window, and "new vs Aug 10 to Sep 6 (n 450)" when the measure does not cover all of it; with no previous window it prints the change alone. */
export function versusPrevious(change: string, previousText: string | null, previousN?: number): string {
	if (!previousText) return change;
	return `${change} vs ${previousText}${previousN === undefined ? '' : ` (n\u00a0${previousN.toLocaleString('en-US')})`}`;
}
