import { describe, expect, it } from 'vitest';
import type { MeasurementRow, SeriesWindows } from '$lib/stores/measurement';
import { versusPrevious } from './measurementSize';
import { ratePeriods, periodChange, changeOnly, periodView as viewOf } from './measurementPeriodView';
import { periodsPeriod } from '../__tests__/fixtures/measurementPeriod';

const SERIES = 'active_hours_per_outcome';
const periodAt = (i: number) => new Date(Date.parse('2026-08-10T00:00:00Z') + i * 7 * 86_400_000).toISOString().slice(0, 10);
const row = (i: number, value: number | null, n = 30, flags: Partial<MeasurementRow> = {}): MeasurementRow => ({
	period: periodAt(i), series: SERIES, value, n, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false, ...flags,
});

/** The headline pools the 4 weeks the served period names, ending with the last row's week. */
const periodView = (rows: MeasurementRow[], series: string, scale = 1, windows?: SeriesWindows) => viewOf(rows, series, scale, periodsPeriod(rows[rows.length - 1].period), windows);
const win = (latest: [number | null, number], previous: [number | null, number]): SeriesWindows => ({ latest: { value: latest[0], n: latest[1] }, previous: { value: previous[0], n: previous[1] } });

describe('versusPrevious', () => {
	it('names the previous window and its n when the comparison reads new, as the render does', () => {
		expect(versusPrevious('new', 'Aug 10 to Sep 6', 494)).toBe('new vs Aug 10 to Sep 6 (n 494)');
		expect(versusPrevious('new', 'Aug 10 to Sep 6')).toBe('new vs Aug 10 to Sep 6');
		expect(versusPrevious('new', null, 494)).toBe('new');
	});
	it('names the previous window and its n for every other reading', () => {
		expect(versusPrevious('-4%', 'Sep 21 to Sep 27', 1451)).toBe('-4% vs Sep 21 to Sep 27 (n 1,451)');
		expect(versusPrevious('low n', 'Sep 21 to Sep 27')).toBe('low n vs Sep 21 to Sep 27');
		expect(versusPrevious('-4%', null, 451)).toBe('-4%');
	});
});

describe('weekly outcome rates', () => {
	it('draws only completed comparable weeks, from the first with a value, converting the unit', () => {
		const periods = ratePeriods([row(0, null, 4), row(1, 0.1), row(2, 0.2, 30, { partial: true }), row(3, 0.3, 30, { comparable: false }), row(4, null, 6)], SERIES, 60);
		expect(periods).toEqual([{ period: periodAt(1), value: 6, n: 30, average: null }, { period: periodAt(4), value: null, n: 6, average: null }]);
	});

	it('reads the headline from the served windows, not from the weeks drawn: a withheld week stays in the served figure', () => {
		const rows = [row(0, 1, 20), row(1, 4, 60), row(2, null, 5), row(3, 2, 20)];
		// The server pooled 105 outcomes (the 5 behind the withheld week included); the drawn weeks alone would pool to 100.
		const view = periodView(rows, SERIES, 1, win([2.5, 105], [null, 0]));
		expect(view.current).toEqual({ value: 2.5, n: 105, periods: [0, 1, 2, 3].map(periodAt) });
		expect(view.previous).toEqual({ value: null, n: 0, periods: [] });
		expect(periodChange(view)).toBe(`new vs ${view.prevText}`);
		expect(view.nowText).toBe('Aug 10 to Sep 6');
		// A window the server pooled only in part (a break left one week) is named by the dates it pooled, not the 4 weeks around them.
		const narrowed: SeriesWindows = { latest: { value: 2, n: 20, from: periodAt(3), to: '2026-09-06' }, previous: { value: null, n: 0 } };
		expect(periodView(rows, SERIES, 1, narrowed).nowText).toBe('Aug 31 to Sep 6');
		// A scale converts the served figure into the reader's unit; with no served window there is no figure and no pool of our own.
		expect(periodView(rows, SERIES, 60, win([2.5, 105], [null, 0])).current.value).toBe(150);
		expect(periodView(rows, SERIES).current).toEqual({ value: null, n: 0, periods: [0, 1, 2, 3].map(periodAt) });
	});

	it('compares the latest four weeks with the four before, and reads low n and new by the one change rule', () => {
		const rows = Array.from({ length: 8 }, (_, i) => row(i, i < 4 ? 2 : 1, 25));
		const view = periodView(rows, SERIES, 1, win([1, 100], [2, 100]));
		expect(periodChange(view)).toBe(`-50% vs ${view.prevText}`);
		expect(periodChange(periodView([row(0, 1, 6), row(1, 1, 6)], SERIES, 1, win([1, 12], [null, 0])))).toBe('low n vs Jun 29 to Jul 26');
		const partlyCovered = periodView([row(0, 2, 25), row(1, 2, 25), row(2, 1, 25), row(3, 1, 25), row(4, 1, 25), row(5, 1, 25)], SERIES, 1, win([1, 100], [1.5, 100]));
		expect(periodChange(partlyCovered)).toBe(`new vs ${partlyCovered.prevText}`);
		expect(changeOnly(partlyCovered)).toBe('new');
	});

	it('names the previous window and its n when the series drew none of it, as it does for a part-covered one', () => {
		// The series begins inside the latest window (the first-try rate on Weeks): nothing is drawn for the previous window, which the server still served.
		const rows = [4, 5, 6, 7].map((i) => row(i, 0.9, 30));
		const view = viewOf(rows, SERIES, 1, periodsPeriod(periodAt(7)), win([0.9, 120], [null, 0]));
		expect(view.prevText).not.toBe('');
		expect(periodChange(view, true)).toBe(`new vs ${view.prevText} (n 0)`);
		expect(changeOnly(view)).toBe('new');
	});

	it('draws a week the server withheld as unknown, and the served moving average with a gap where it serves none', () => {
		const served = (i: number, average: number | null, value: number | null = 1) => row(i, value, 30, { average });
		const view = periodView([served(0, null), served(1, null), served(2, null), served(3, 1), served(4, null, null), served(5, 2)], SERIES);
		expect(view.points.map((p) => p.ma)).toEqual([null, null, null, 1, null, 2]);
		expect(periodView([row(0, null, 3)], SERIES).periods).toEqual([]);
	});

	it('draws the moving average as served, converted to the reader\'s unit and never recomputed from the rows', () => {
		const rows = [row(0, 1, 30, { average: 0.5 }), row(1, 3, 30, { average: 1 }), row(2, 5, 30)];
		expect(periodView(rows, SERIES).points.map((p) => p.ma)).toEqual([0.5, 1, null]);
		expect(periodView(rows, SERIES, 60).points.map((p) => p.ma)).toEqual([30, 60, null]);
	});

	it('reads a count headline from the served window', () => {
		const rows = Array.from({ length: 8 }, (_, i) => row(i, i < 4 ? 10 : 20, i < 4 ? 10 : 20));
		const view = viewOf(rows, SERIES, 1, periodsPeriod(periodAt(7)), win([80, 80], [40, 40]));
		expect(view.current).toEqual({ value: 80, n: 80, periods: [4, 5, 6, 7].map(periodAt) });
		expect(view.previous?.value).toBe(40);
	});
});
