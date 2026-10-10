import { type MeasurementRow, type SeriesWindows, type WindowFigure, type WindowSize } from '$lib/stores/measurement';
import { drawnValue } from '$lib/stores/measurementRows';
import type { BarPoint } from './measurementBarChart';
import { barPoints, changeText, inSpan, sizePeriods, spanPeriods, versusPrevious, type PeriodSize } from './measurementSize';
import { periodName, type MeasurementPeriod } from '$lib/stores/measurementPeriod';

/** One period (a day or a week) of a served outcome rate. The server withholds the value when too few outcomes stand behind it; the period then carries its raw value, drawn hollow. */
export interface RatePeriod { period: string; value: number | null; n: number; /** The served moving average at this period, in the same unit as the value. */ average: number | null; /** The period still running, drawn but never counted in a window (Days only). */ partial?: boolean }

/** One comparison window: the served pooled rate, the outcomes behind it, the periods drawn inside it and, for the size series, the counts the server pooled. */
export interface RateWindow { value: number | null; n: number; periods: string[]; size?: WindowSize }

/** The served latest and previous windows, and the dates that name them. */
export interface WindowPair { current: RateWindow; previous: RateWindow | null; nowText: string; prevText: string; previousExpected: number }

export interface PeriodView extends WindowPair { periods: RatePeriod[]; points: BarPoint[] }

/**
 * Completed, comparable periods of one series, from the first with a value on. The server marks a period partial while
 * it is running and not comparable when it was counted differently; neither is drawn as complete, the same rule the other panels use.
 * On Days alone a running day that has a value is kept, marked partial.
 */
export function ratePeriods(rows: MeasurementRow[], series: string, scale = 1, daily = false): RatePeriod[] {
	const kept: RatePeriod[] = rows.filter((row) => row.series === series && row.comparable && (!row.partial || (daily && row.value !== null)))
		.sort((a, b) => a.period.localeCompare(b.period))
		.map((row) => {
			const drawn = drawnValue(row);
			return { period: row.period, value: drawn === null ? null : drawn * scale, n: row.n, average: row.average == null ? null : row.average * scale, ...(row.partial ? { partial: true } : {}) };
		});
	const first = kept.findIndex((w) => w.value !== null);
	return first < 0 ? [] : kept.slice(first);
}

/** A Days series ends at its last day with something recorded: completed days after it with no value and nothing behind them (n 0) are not drawn. A day whose value is withheld but has n above 0 is real and stays. A running day is kept. */
export function dropTrailingEmptyDays(periods: RatePeriod[]): RatePeriod[] {
	if (periods[periods.length - 1]?.partial) return periods;
	let end = periods.length;
	while (end > 0 && periods[end - 1].value === null && periods[end - 1].n === 0) end--;
	return periods.slice(0, end);
}

/** One bar per period, and the moving average the server served for it; a period without one draws no point on the line. */
export function ratePoints(periods: RatePeriod[]): BarPoint[] {
	return periods.map((w) => ({ start: w.period, value: w.value, n: w.n, ma: w.average, ...(w.partial ? { partial: true } : {}) }));
}

/** A served window figure in the unit the reader sees; with no served figure the headline has nothing to say and reads n/a. */
const served = (figure: WindowFigure | undefined, periods: string[], scale: number): RateWindow => ({
	value: figure?.value == null ? null : figure.value * scale, n: figure?.n ?? 0, periods, ...(figure?.size ? { size: figure.size } : {}),
});

/**
 * Every headline's window figures: the served pooled figure for the latest and previous windows (the series response's `windows`
 * block), never a pool of the drawn rows. `drawn` (completed periods, in order) only names which periods stand behind each window,
 * so a previous window the series covers only in part reads new.
 */
export function windowPair(drawn: string[], period?: MeasurementPeriod, windows?: SeriesWindows, scale = 1): WindowPair {
	const cur = period ? drawn.filter((key) => inSpan(key, period.latest)) : [], prev = period ? drawn.filter((key) => inSpan(key, period.previous)) : [];
	return {
		current: served(windows?.latest, cur, scale), previous: prev.length && windows?.previous ? served(windows.previous, prev, scale) : null,
		// The latest figure is named by the dates the server pooled for it, never by a wider window it did not pool.
		nowText: period ? periodName(windows?.latest?.from && windows.latest.to ? { from: windows.latest.from, to: windows.latest.to } : period.latest) : '',
		prevText: period && prev.length ? periodName(period.previous) : '',
		// With no served period there is no previous window to cover, so nothing reads new for covering it in part.
		previousExpected: period ? spanPeriods(period, period.previous) : 0,
	};
}

/** One outcome-rate panel's drawn periods and its served windows; a day the server withheld still counts in the window. */
export function periodView(rows: MeasurementRow[], series: string, scale = 1, period?: MeasurementPeriod, windows?: SeriesWindows): PeriodView {
	const daily = period?.grain === 'days';
	const periods = ratePeriods(rows, series, scale, daily);
	const pair = windowPair(periods.filter((w) => !w.partial).map((w) => w.period), period, windows, scale);
	return { periods, points: ratePoints(daily ? dropTrailingEmptyDays(periods) : periods), ...pair, nowText: periods.length ? pair.nowText : '' };
}

export interface SizeView extends WindowPair { periods: PeriodSize[]; points: BarPoint[] }

/** The size panel: its drawn run of periods and the server's pooled windows, read by the same rule as every other measure. */
export function sizeView(rows: MeasurementRow[], period?: MeasurementPeriod, windows?: SeriesWindows): SizeView {
	const periods = sizePeriods(rows);
	return { periods, points: barPoints(periods), ...windowPair(periods.map((w) => w.period), period, windows) };
}

/**
 * "-4% vs Sep 21 to Sep 27": the one change rule, with no previous window, or one the series covers only in part, it reads new.
 * `figure` reads another measure from the same two windows; `minimum` is the series' served minimum n where it has one.
 */
export function periodChange(view: WindowPair, withPreviousN = false, figure: (w: RateWindow) => number | null = (w) => w.value, minimum?: number): string {
	const { previous } = view;
	return versusPrevious(changeOnly(view, figure, minimum), previous ? view.prevText : null, withPreviousN && previous ? previous.n : undefined);
}

/** The change alone ("-69%", "new", "low n"), without the window it is against, for a line that names no windows. */
export function changeOnly(view: WindowPair, figure: (w: RateWindow) => number | null = (w) => w.value, minimum?: number): string {
	const { current, previous } = view;
	return changeText(figure(current), previous ? figure(previous) : null, current.n, previous?.n ?? 0, !!previous && previous.periods.length < view.previousExpected, minimum);
}
