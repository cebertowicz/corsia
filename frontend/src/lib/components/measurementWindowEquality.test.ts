import { describe, expect, it } from 'vitest';
import type { MeasurementRow } from '$lib/stores/measurement';
import { periodChange, periodView, sizeView } from './measurementPeriodView';
import { costView } from './measurementCostRate';
import { SIZE_SERIES } from './measurementSize';
import { daysPeriod, periodsPeriod } from '../__tests__/fixtures/measurementPeriod';
import type { MeasurementPeriod } from '$lib/stores/measurementPeriod';

// A rate headline is the served `windows` figure, never a pool of the rows drawn: it must equal the served latest and previous
// window at both grains even when the drawn rows would pool to something else (the server counts the days it withheld).
type Grain = 'days' | 'weeks';
const stepDays = (grain: Grain) => (grain === 'days' ? 1 : 7);
const at = (grain: Grain, i: number) => new Date(Date.parse('2026-08-10T00:00:00Z') + i * stepDays(grain) * 86_400_000).toISOString().slice(0, 10);
const COUNT = 14;
const periodFor = (grain: Grain): MeasurementPeriod => (grain === 'days' ? daysPeriod(at(grain, COUNT - 1)) : periodsPeriod(at(grain, COUNT - 1)));
const base = { proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false } as const;

function rateRows(grain: Grain, series: string): MeasurementRow[] {
	return Array.from({ length: COUNT }, (_, i) => ({ ...base, period: at(grain, i), series, value: i % 5 === 3 ? null : 0.1 + i * 0.01, n: 20 + i }));
}

describe.each(['days', 'weeks'] as const)('headline pools equal the served latest window at %s', (grain) => {
	const period = periodFor(grain);
	it.each([
		['minutes per outcome', 'active_hours_per_outcome', 60],
		['asks per outcome', 'asks_per_outcome', 1],
		['first try', 'first_try_acceptance', 1],
	])('%s', (_name, series, scale) => {
		const rows = rateRows(grain, series);
		const windows = { latest: { value: 0.321, n: 321 }, previous: { value: 0.123, n: 123 } };
		const view = periodView(rows, series, scale, period, windows);
		expect(view.current.value).toBeCloseTo(0.321 * scale, 10);
		expect(view.current.n).toBe(321);
		expect(view.previous?.value).toBeCloseTo(0.123 * scale, 10);
		expect(view.previous?.n).toBe(123);
		expect(view.current.periods.length).toBe(grain === 'days' ? 7 : 4);
	});

	it('size hours per hour you spend', () => {
		const rows = Array.from({ length: COUNT }, (_, i) => ({
			...base, period: at(grain, i), series: SIZE_SERIES, value: 2 + i, n: 10 + i,
			size: {
				outcomes: 12 + i, sized: 10 + i, estimated_hours: 3 * (i + 1), active_hours: 1 + i / 2, unsized: 2, unknown: 0, stale: 0, conflict: 0, retracted: 0, malformed: 0,
				basis: { delivered_evidence_proxy: 10 + i, accepted_scope_proxy: 0, author_declared: 0 }, profiles: [], comparable_reason: null, rate_reason: null,
				class_midpoint_hours: { XS: 0.1, S: 0.5, M: 2, L: 8, XL: 40, XXL: 120 }, data_start: at(grain, 0), comparability_before: at(grain, 0),
			},
		})) as unknown as MeasurementRow[];
		// The served windows pool to figures the drawn rows would not: the headline reads the served ones, counts and all.
		const windows = {
			latest: { value: 32.1, n: 321, size: { outcomes: 400, sized: 321, excluded: 79, estimated_hours: 642, active_hours: 20 } },
			previous: { value: 12.3, n: 123, size: { outcomes: 150, sized: 123, excluded: 27, estimated_hours: 246, active_hours: 20 } },
		};
		const view = sizeView(rows, period, windows);
		expect(view.current.value).toBe(32.1);
		expect(view.current.n).toBe(321);
		expect(view.current.size?.excluded).toBe(79);
		expect(view.previous?.value).toBe(12.3);
		expect(view.previous?.n).toBe(123);
	});
});

describe.each(['days', 'weeks'] as const)('a previous window with no data reads new, never 0 or -100%, at %s', (grain) => {
	const period = periodFor(grain);
	const rows = (series: string): MeasurementRow[] => Array.from({ length: COUNT }, (_, i) => {
		const inPrevious = at(grain, i) >= period.previous.from && at(grain, i) <= period.previous.to;
		return { ...base, period: at(grain, i), series, value: inPrevious ? null : 0.3, n: inPrevious ? 0 : 40 };
	});

	it('reads new for an outcome rate', () => {
		const view = periodView(rows('first_try_acceptance'), 'first_try_acceptance', 1, period, { latest: { value: 0.3, n: 40 }, previous: { value: null, n: 0 } });
		expect(view.current.value).toBeCloseTo(0.3, 10);
		expect(periodChange(view)).toMatch(/^new\b/);
		expect(periodChange(view)).not.toMatch(/0%|-100%/);
	});

	it('reads new for the cost per hour', () => {
		const figures = (to: string, dollars: number | null, sized: number) => ({ from: to, to, dollars, priced_records: 10, api_records: 10, outcomes: sized, per_outcome: null, sized, hours: sized * 2, per_hour: dollars === null ? null : dollars / (sized * 2) });
		const latest = figures(period.latest.to, 800, 40), previous = figures(period.previous.to, null, 0);
		const view = costView({ series: [{ ...previous, period: previous.to, average: null }, { ...latest, period: latest.to, average: null }], latest, previous, period });
		expect(view?.change).toMatch(/^new\b/);
		expect(view?.change).not.toMatch(/0%|-100%/);
	});
});

describe.each(['days', 'weeks'] as const)('the cost change follows the one partial-previous-window rule at %s', (grain) => {
	const period = periodFor(grain);
	const figures = (to: string, dollars: number, sized: number) => ({ from: to, to, dollars, priced_records: 10, api_records: 10, outcomes: sized, per_outcome: null, sized, hours: sized * 2, per_hour: dollars / (sized * 2) });
	const latest = figures(period.latest.to, 800, 40), previous = figures(period.previous.to, 400, 40);
	const served = (from: string) => Array.from({ length: COUNT }, (_, i) => at(grain, i)).filter((key) => key >= from)
		.map((key) => ({ ...figures(key, 100, 40), period: key, average: null }));

	it('reads new when the series began inside the previous window, even with a previous figure, as the outcome rates do', () => {
		const began = served(period.previous.to);
		const view = costView({ series: began, latest, previous, period });
		expect(view?.change).toMatch(/^new( vs .+)?$/);
		const rate = periodView(began.map((w) => ({ ...base, period: w.period, series: 'first_try_acceptance', value: 0.3, n: 40 })), 'first_try_acceptance', 1, period,
			{ latest: { value: 0.6, n: 40 }, previous: { value: 0.3, n: 40 } });
		expect(periodChange(rate, true)).toBe(view?.change);
	});

	it('compares a previous window the series covers in full', () => {
		expect(costView({ series: served(period.previous.from), latest, previous, period })?.change).toMatch(/^\+100% vs /);
	});
});
