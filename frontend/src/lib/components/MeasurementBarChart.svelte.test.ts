import { afterEach, describe, expect, it } from 'vitest';
import { mount, unmount } from 'svelte';
import MeasurementBarChart from './MeasurementBarChart.svelte';
import { desktopGeometry, niceAxis, phoneGeometry, type BarPoint } from './measurementBarChart';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

const period = (i: number) => new Date(Date.parse('2026-08-10T00:00:00Z') + i * 7 * 86_400_000).toISOString().slice(0, 10);
const point = (i: number, value: number | null, n = 100, ma: number | null = null): BarPoint => ({ start: period(i), value, n, ma });
const props = (points: BarPoint[]) => ({
	points, axisTitle: 'Hours of work per hour you spend', unit: 'week', averageName: '4-week moving average', minN: 20, minNText: 'fewer than 20 outcomes',
	caption: 'Estimated hours of work delivered divided by your engaged hours; higher is better. n is size-rated accepted outcomes per week.',
	ariaLabel: 'Hours of work per hour you spend, Aug 10 to Oct 4', tickFormat: (v: number, d: number) => v.toFixed(d), valueFormat: (v: number) => v.toFixed(0),
});
function render(points: BarPoint[]): HTMLElement {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementBarChart, { target: host, props: props(points) });
	return host.querySelector('.chart') as HTMLElement;
}
const texts = (root: Element, cls: string) => [...root.querySelectorAll(`text.${cls}`)].map((n) => n.textContent);

describe('MeasurementBarChart', () => {
	it('keeps a single digit upright on the phone chart', () => {
		const chart = render([point(0, 8, 50), point(1, 12, 50)]);
		const labels = chart.querySelectorAll('.mob svg text.vl');
		expect(labels[0].textContent).toBe('8');
		expect(labels[0].hasAttribute('transform')).toBe(false);
		expect(labels[1].getAttribute('transform')).toMatch(/^rotate\(-90 /);
	});

	it('draws axis title, ticks, a zero baseline, a value on every bar, the n row and a caption', () => {
		const chart = render([point(0, 30, 50), point(1, 45, 80), point(2, 92, 1349)]);
		const phone = chart.querySelector('.mob svg')!;
		expect(texts(phone, 'at')).toEqual(['Hours of work per hour you spend']);
		expect(texts(phone, 'tk')).toEqual(['0', '25', '50', '75', '100']);
		expect(phone.querySelectorAll('line.zero')).toHaveLength(1);
		expect(texts(phone, 'vl')).toEqual(['30', '45', '92']);
		expect(texts(phone, 'nn')).toEqual(['n', '50', '80', '1,349']);
		expect(phone.querySelectorAll('rect.bar')).toHaveLength(3);
		expect(phone.querySelector('rect.bar.cur')).not.toBeNull();
		expect(chart.querySelector('figcaption')?.textContent).toContain('higher is better');
		expect(chart.querySelector('.lg')?.textContent).toContain('One week');
		expect(chart.querySelector('.lg')?.textContent).not.toContain('Hollow bar');
		// Phone labels are rotated; the desktop chart keeps them level.
		expect(phone.querySelector('text.vl')?.getAttribute('transform')).toMatch(/^rotate\(-90 /);
		expect(chart.querySelector('.dsk svg text.vl')?.getAttribute('transform')).toBeNull();
	});

	it('draws a bar under the minimum n hollow, labelled low n, and cuts a taller one at the axis', () => {
		const chart = render([point(0, 40, 90), point(1, 41, 90), point(2, 200, 5)]);
		const phone = chart.querySelector('.mob svg')!;
		expect(phone.querySelectorAll('rect.bar.low')).toHaveLength(1);
		expect(texts(phone, 'lo')).toEqual(['low n']);
		expect(texts(phone, 'vl')).toEqual(['40', '41', '200']);
		expect(phone.querySelector('path.brk')).not.toBeNull();
		expect(chart.querySelector('.lg')?.textContent).toContain('Hollow bar: fewer than 20 outcomes (low n); a bar taller than the axis is cut at the top and keeps its value');
		// The axis is scaled to the bars at or above the minimum (41 needs 60), not to the 200 bar.
		expect(texts(phone, 'tk').at(-1)).toBe('60');
	});

	it('labels a withheld period that has n above 0 as too few, under its own n on one line, never as no data', () => {
		const chart = render([point(0, 30, 40), point(1, null, 5), point(2, null, 0), point(3, 50, 12)]);
		for (const g of ['.mob svg', '.dsk svg']) {
			const svg = chart.querySelector(g)!;
			expect(svg.querySelectorAll('rect.bar')).toHaveLength(2);
			expect(texts(svg, 'lo')).toEqual(['too few', 'low n']);
			expect(texts(svg, 'nn')).toEqual(['n', '40', '5', '0', '12']);
			expect(new Set([...svg.querySelectorAll('text.nn')].map((n) => n.getAttribute('y'))).size).toBe(1);
		}
		expect(chart.querySelector('.lg')?.textContent).toContain('too few');
		// Each state label starts at its own bar's x and reads downward on the phone.
		const phone = chart.querySelector('.mob svg')!;
		const xs = [...phone.querySelectorAll('text.nn')].slice(1).map((n) => Number(n.getAttribute('x')));
		const states = [...phone.querySelectorAll('text.lo')];
		expect(states.map((n) => n.getAttribute('transform'))).toEqual([expect.stringMatching(/^rotate\(90 /), expect.stringMatching(/^rotate\(90 /)]);
		expect(states.map((n) => Number(n.getAttribute('x')))).toEqual([xs[1] - 3.5, xs[3] - 3.5]);
	});

	it('draws the moving average only for periods whose window is inside the data, and haloes the labels', () => {
		const chart = render([point(0, 30), point(1, 31), point(2, 32), point(3, 33, 100, 31.5), point(4, 34, 100, 32.5)]);
		const phone = chart.querySelector('.mob svg')!;
		expect(phone.querySelector('polyline.ma')?.getAttribute('points')?.split(' ')).toHaveLength(2);
		expect(chart.querySelector('.lg')?.textContent).toContain('4-week moving average');
		// Labels follow the line in the DOM so the line never strikes through them.
		const order = [...phone.children].map((n) => (n.getAttribute('class') ?? '').split(' ')[0]);
		expect(order.indexOf('ma')).toBeLessThan(order.lastIndexOf('vl'));
		const none = render([point(0, 30), point(1, 31)]);
		expect(none.querySelector('polyline.ma')).toBeNull();
		expect(none.querySelector('.lg')?.textContent).not.toContain('moving average');
	});

	it('breaks the moving average at a period with none, so the line never bridges the gap or falls to zero', () => {
		const chart = render([point(0, 30, 100, 30), point(1, 31, 100, 31), point(2, 32, 100, null), point(3, 33, 100, 33), point(4, 34, 100, 34), point(5, 35, 100, null), point(6, 36, 100, 36)]);
		for (const view of ['.mob svg', '.dsk svg']) {
			const svg = chart.querySelector(view)!;
			expect([...svg.querySelectorAll('polyline.ma')].map((line) => line.getAttribute('points')?.split(' ').length)).toEqual([2, 2]);
			expect(svg.querySelectorAll('circle.ma')).toHaveLength(1);
			// Nothing on the line sits at the baseline, where a null read as zero would put it.
			const baseline = Number(svg.querySelector('line.zero')!.getAttribute('y1'));
			expect([...svg.querySelectorAll('polyline.ma')].flatMap((line) => line.getAttribute('points')!.split(' ')).every((xy) => Number(xy.split(',')[1]) < baseline - 5)).toBe(true);
		}
	});

	it('names the average in the legend and the screen-reader table, with none where the server served none', () => {
		const chart = render([point(0, 30, 100, null), point(1, 31, 100, 30.5), point(2, null, 0, null)]);
		expect(chart.querySelector('.lg')?.textContent).toContain('4-week moving average');
		const rows = [...chart.querySelectorAll('table.sr-only tr')].map((row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent));
		expect(rows[0]).toEqual(['Week starting', 'Hours of work per hour you spend', 'n', '4-week moving average']);
		expect(rows.slice(1).map((cells) => cells[3])).toEqual(['none', '31', 'none']);
		const none = render([point(0, 30), point(1, 31)]);
		expect(none.querySelector('table.sr-only thead')?.textContent).not.toContain('moving average');
	});

	it('starts at the first recorded period and shows an unknown period with n 0 as no bar and no label, never a zero bar', () => {
		const chart = render([point(0, 30, 40), point(1, null, 0), point(2, null, 0), point(3, 50, 60)]);
		const phone = chart.querySelector('.mob svg')!;
		expect(phone.querySelectorAll('rect.bar')).toHaveLength(2);
		expect(texts(phone, 'lo')).toEqual([]);
		expect(chart.querySelector('.lg')?.textContent).not.toContain('too few');
		expect(texts(phone, 'vl')).toEqual(['30', '50']);
		expect(texts(phone, 'nn')).toEqual(['n', '40', '0', '0', '60']);
	});

	it('sizes the 393 px chart as the render does', () => {
		const spec = { points: [point(0, 30), point(1, 92)], axisTitle: 'x', tickFormat: (v: number) => String(v), valueFormat: (v: number) => String(v), minN: 20 };
		expect(phoneGeometry(spec).width).toBe(335);
		expect(Math.trunc(phoneGeometry(spec).height)).toBe(343);
		expect(desktopGeometry(spec).width).toBe(760);
		expect(desktopGeometry(spec, 1180).width).toBe(1180);
		expect(Math.trunc(desktopGeometry(spec).height)).toBe(249);
		expect(niceAxis(92)).toEqual({ step: 25, count: 4 });
	});

	it('reads every week to a screen reader with the same formatter as the bar label, never the raw number', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(MeasurementBarChart, { target: host, props: { ...props([point(0, 429.7701192, 50), point(1, null, 3), point(2, 0.855, 10)]), valueFormat: (v: number) => v.toFixed(1) } });
		const rows = [...host.querySelectorAll('table.sr-only tbody tr')].map((tr) => [...tr.children].map((cell) => cell.textContent));
		expect(rows).toEqual([['2026-08-10', '429.8', '50'], ['2026-08-17', 'no value', '3'], ['2026-08-24', '0.9, fewer than 20 outcomes', '10']]);
		expect(host.textContent).not.toContain('429.77');
	});
});
