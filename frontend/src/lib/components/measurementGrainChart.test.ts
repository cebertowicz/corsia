import { describe, expect, it } from 'vitest';
import type { BarPoint } from './measurementBarChart';
import { desktopGeometry, phoneGeometry } from './measurementBarChart';
import { grainChart } from './measurementSize';
import { daysPeriod, periodsPeriod } from '../__tests__/fixtures/measurementPeriod';

const point = (start: string, value: number | null, n = 30, ma: number | null = null): BarPoint => ({ start, value, n, ma });
const EIGHT_WEEKS = ['2026-08-10', '2026-08-17', '2026-08-24', '2026-08-31', '2026-09-07', '2026-09-14', '2026-09-21', '2026-09-28'];

describe('the shared Weeks axis', () => {
	it('draws every Weeks chart on the last eight completed weeks, whatever week its measure started', () => {
		const late = grainChart(periodsPeriod('2026-09-28'), [point('2026-09-14', 89), point('2026-09-21', 88), point('2026-09-28', 90)], 4);
		expect(late.points.map((p) => p.start)).toEqual(EIGHT_WEEKS);
		expect(late.points.slice(0, 5).every((p) => p.empty && p.value === null && p.ma === null)).toBe(true);
		expect(late.points.slice(5).map((p) => p.value)).toEqual([89, 88, 90]);
		expect(late.rangeText).toBe('Aug 10 to Oct 4');
	});

	it('leaves out weeks the server read before the axis, so no chart runs longer than eight', () => {
		const long = grainChart(periodsPeriod('2026-09-28'), ['2026-07-27', '2026-08-03', ...EIGHT_WEEKS].map((start) => point(start, 5)), 4);
		expect(long.points.map((p) => p.start)).toEqual(EIGHT_WEEKS);
		expect(long.points.some((p) => p.empty)).toBe(false);
	});

	it('leaves Days exactly as served', () => {
		const days = [point('2026-10-01', 3), point('2026-10-02', 4)];
		const chart = grainChart(daysPeriod('2026-10-08'), days, 7);
		expect(chart.points).toEqual(days);
		expect(chart.rangeText).toBe('Oct 1 to Oct 8');
	});

	it('draws an empty week as its date alone: no bar, no n, no state, no average point', () => {
		const { points } = grainChart(periodsPeriod('2026-09-28'), [point('2026-09-21', 4, 30, 4), point('2026-09-28', 5, 30, 4.5)], 4);
		for (const geometry of [desktopGeometry({ points, axisTitle: 'x', tickFormat: String, valueFormat: String, minN: 20 }), phoneGeometry({ points, axisTitle: 'x', tickFormat: String, valueFormat: String, minN: 20 })]) {
			const of = (cls: string) => geometry.items.filter((item) => item.kind === 'text' && item.cls.split(' ')[0] === cls);
			expect(geometry.items.filter((item) => item.kind === 'rect')).toHaveLength(2);
			expect(of('xl')).toHaveLength(8);
			// The n row is its label plus one n per drawn bar.
			expect(of('nn')).toHaveLength(3);
			expect(of('lo')).toHaveLength(0);
			expect(geometry.items.filter((item) => item.kind === 'polyline')).toHaveLength(1);
		}
	});
});
