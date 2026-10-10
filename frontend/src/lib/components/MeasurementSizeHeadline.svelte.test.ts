import { afterEach, describe, expect, it } from 'vitest';
import { mount, unmount } from 'svelte';
import MeasurementSizeHeadline from './MeasurementSizeHeadline.svelte';
import type { MeasurementRow, PanelState, SeriesWindows, SizeCoverage, WindowFigure, WindowSize } from '$lib/stores/measurement';
import { periodsPeriod } from '../__tests__/fixtures/measurementPeriod';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

const coverage = (overrides: Partial<SizeCoverage>): SizeCoverage => ({
	outcomes: 30, sized: 3, estimated_hours: 6, active_hours: 0.2, unsized: 27, unknown: 0, stale: 0, conflict: 0, retracted: 0, malformed: 0,
	basis: { delivered_evidence_proxy: 3, accepted_scope_proxy: 0, author_declared: 0 },
	profiles: [{ method: 'offline_proxy', method_version: '1', rubric_version: '1.3p', rater: 'synthetic-rater', rater_version: '2026-10-01', outcomes: 3 }],
	comparable_reason: null, rate_reason: null, class_midpoint_hours: { XS: 0.1, S: 0.5, M: 2, L: 8, XL: 40, XXL: 120 },
	data_start: '2026-08-03', comparability_before: '2026-08-31', ...overrides,
});

const WEEK0 = Date.parse('2026-08-10T00:00:00Z');
const periodAt = (i: number) => new Date(WEEK0 + i * 7 * 86_400_000).toISOString().slice(0, 10);
/** A served week: value null means the server withheld the rate. */
const at = (i: number, value: number | null, overrides: Partial<SizeCoverage> = {}, flags: Partial<MeasurementRow> = {}): MeasurementRow => {
	const size = coverage({ outcomes: 30, sized: 30, unsized: 0, estimated_hours: (value ?? 0) * 0.4, active_hours: 0.4, ...overrides });
	return { period: periodAt(i), series: 'estimated_human_hours_per_active_hour', value, n: size.sized, proxy: true, partial: size.sized < size.outcomes,
		comparable: true, break: null, dimension: null, thin: false, size, ...flags };
};

/** A served window figure: the headline prints exactly this, never a pool of the rows it draws. */
const win = (value: number | null, n: number, size: Partial<WindowSize> = {}): WindowFigure => ({
	value, n, size: { outcomes: n, sized: n, excluded: 0, estimated_hours: 0, active_hours: 0, ...size },
});
const NONE: SeriesWindows = { latest: win(null, 0), previous: win(null, 0) };

function mountRows(rows: MeasurementRow[], windows: SeriesWindows = NONE): HTMLElement {
	const state: PanelState = { loading: false, rows, error: null, selection: 'weeks:all:none', period: periodsPeriod(rows[rows.length - 1].period), averagePeriods: 4,
		windows: { estimated_human_hours_per_active_hour: windows } };
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementSizeHeadline, { target: host, props: { state, onRetry: () => {} } });
	return host.querySelector('.sizing-headline') as HTMLElement;
}
function render(size: SizeCoverage, value: number | null = null, comparable = true, windows: SeriesWindows = NONE): HTMLElement {
	const row: MeasurementRow = { period: '2026-09-28', series: 'estimated_human_hours_per_active_hour', value, n: size.sized, proxy: true,
		partial: size.sized < size.outcomes, comparable, break: null, dimension: null, thin: false, size };
	return mountRows([row], windows);
}
const bars = (el: HTMLElement) => [...el.querySelectorAll('.mob svg .bar')];

describe('MeasurementSizeHeadline', () => {
	it('is titled with the plain measure name and ordered title, answer, chart, why it moved', () => {
		const el = mountRows(Array.from({ length: 8 }, (_, i) => at(i, 40 + i)));
		expect(el.querySelector('#sizing-title')?.textContent).toBe('Work per hour of your attention');
		const order = ['#sizing-title', '.stat', '.chart', '.why'].map((q) => el.querySelector(q)!);
		order.forEach((node, i) => { if (i) expect(order[i - 1].compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy(); });
		expect(el.textContent).not.toContain('?');
		expect(el.querySelector('.why h4')?.textContent).toContain('Why it moved, size-rated outcomes only');
		expect([...el.querySelectorAll('.why .kl')].map((n) => n.textContent)).toEqual(['Hours of work per size-rated outcome', 'Your minutes per size-rated outcome']);
	});

	it('states the answer over the last four weeks against the four before, with n', () => {
		// The served windows pool four weeks of 30 sized outcomes each over 0.4 active hours a week.
		const el = mountRows(Array.from({ length: 8 }, (_, i) => at(i, i < 4 ? 40 : 60)), {
			latest: win(60, 120, { estimated_hours: 96, active_hours: 1.6 }), previous: win(40, 120, { estimated_hours: 64, active_hours: 1.6 }),
		});
		expect(el.querySelector('.stat')?.textContent).toBe('60 h of work per hour you spend, Sep 7 to Oct 4, n 120, +50% vs Aug 10 to Sep 6 (n 120)');
		expect([...el.querySelectorAll('.why .kv')].map((n) => n.textContent)).toEqual(['0.8', '0.8']);
	});

	it('draws the moving average the server served for each week, and none where it served none', () => {
		const el = mountRows(Array.from({ length: 6 }, (_, i) => at(i, 40 + i, {}, { average: i < 3 ? null : 40 + i - 1.5 })));
		const points = el.querySelector('.mob svg polyline.ma')?.getAttribute('points')?.split(' ');
		expect(points).toHaveLength(3);
		expect(el.querySelector('.lg')?.textContent).toContain('4-week moving average');
		expect(el.querySelector('figcaption')?.textContent).toContain('the line is the 4-week moving average');
	});

	it('draws a week with too few sized outcomes hollow with its raw rate and low n, and scales the axis to the full bars', () => {
		const lowPeriod = (i: number) => at(i, null, { sized: 6, outcomes: 30, unsized: 24, estimated_hours: 6 * 4, active_hours: 0.4 }, { raw_value: 120, partial: true });
		const el = mountRows([...Array.from({ length: 2 }, (_, i) => at(i, 40 + i)), lowPeriod(2), ...Array.from({ length: 5 }, (_, i) => at(i + 3, 42))]);
		expect(el.querySelectorAll('.mob svg rect.bar.low')).toHaveLength(1);
		expect([...el.querySelectorAll('.mob svg text.lo')].map((n) => n.textContent)).toEqual(['low n']);
		expect([...el.querySelectorAll('.mob svg text.vl')].map((n) => n.textContent)).toHaveLength(8);
		expect(el.querySelector('.lg')?.textContent).not.toContain('too few');
		// The full bars top out at 42, so the axis stops at 60 and the hollow week, which reads 120, is cut with a break mark and keeps its value.
		expect([...el.querySelectorAll('.mob svg text.tk')].map((n) => n.textContent).pop()).toBe('60');
		expect(el.querySelectorAll('.mob svg path.brk')).toHaveLength(1);
		expect([...el.querySelectorAll('.mob svg text.vl')].map((n) => n.textContent)[2]).toBe('120');
	});

	it('draws nothing before its first recorded week on the eight-week axis and keeps a later unknown week unknown, not zero', () => {
		const el = mountRows([at(0, null, { sized: 0, estimated_hours: 0, rate_reason: 'no_usable_ratings' }), at(1, 30), at(2, null, { sized: 3, rate_reason: 'below_minimum' }), at(3, 50)]);
		expect(el.querySelectorAll('.mob svg .xl')).toHaveLength(8);
		// The n row is its label and one n for each of the three weeks from the first recorded one on.
		expect([...el.querySelectorAll('.mob svg text.nn')].map((n) => n.textContent)).toHaveLength(4);
		expect([...el.querySelectorAll('figure.chart table.sr-only tbody th')].map((n) => n.textContent)).toEqual([periodAt(1), periodAt(2), periodAt(3)]);
		expect(bars(el)).toHaveLength(2);
		expect([...el.querySelectorAll('.mob svg .lo')].map((n) => n.textContent)).toEqual(['too few']);
	});

	it('shows a ratio with zero sized outcomes as undefined, never zero', () => {
		const el = mountRows([at(0, null, { outcomes: 0, sized: 0, estimated_hours: 0, active_hours: 0, unsized: 0, rate_reason: 'no_active_time' })]);
		expect(el.querySelector('.chart')).toBeNull();
		expect(el.querySelector('.stat b')?.textContent).toBe('–');
		expect(el.querySelector('.status')?.textContent).toBe('No active time recorded this week, so no rate.');
		expect([...el.querySelectorAll('.why .kv')].map((n) => n.textContent)).toEqual(['–', '–']);
		expect(el.textContent).not.toMatch(/\b0 h of work/);
	});

	it('reads new for a measure recorded only part of the previous window, even with a figure for it', () => {
		// The server serves no week before the data start, so the previous window holds two of its four weeks.
		const el = mountRows(Array.from({ length: 6 }, (_, i) => at(i + 2, i < 2 ? 40 : 60, { data_start: periodAt(2) })), {
			latest: win(60, 120, { estimated_hours: 96, active_hours: 1.6 }), previous: win(40, 60, { estimated_hours: 32, active_hours: 0.8 }),
		});
		expect(el.querySelector('.stat')?.textContent).toBe('60 h of work per hour you spend, Sep 7 to Oct 4, n 120, new');
		expect([...el.querySelectorAll('.why .kc')].map((n) => n.textContent)).toEqual(['new', 'new']);
	});

	it('prints no rate and n/a when the served window is under the minimum, with its n', () => {
		const few = mountRows([at(0, 10, { sized: 5 })], { latest: win(null, 5), previous: win(null, 0) });
		expect(few.querySelector('.stat')?.textContent).toBe('– h of work per hour you spend, Jul 20 to Aug 16, n 5, n/a');
	});

	it('does not chart weeks served before a comparability break', () => {
		const el = mountRows([at(0, 30, {}, { comparable: false }), at(1, 31), at(2, 32)]);
		expect(bars(el)).toHaveLength(2);
		expect([...el.querySelectorAll('.mob svg text.vl')].map((n) => n.textContent)).toEqual(['31', '32']);
	});

	it('shows a non-comparable latest week on its own, with its rate and no chart', () => {
		const el = mountRows([at(0, 30, {}, { comparable: false }), at(1, 31, {}, { comparable: false })], { latest: win(31, 30), previous: win(null, 0) });
		expect(el.querySelector('.stat')?.textContent).toContain('31 h of work per hour you spend, Jul 27 to Aug 23');
		expect(el.querySelector('.comparability')).not.toBeNull();
		expect(el.querySelector('.mob')).toBeNull();
	});

	it('names the served data start and comparability break, never a copied date', () => {
		const before = render(coverage({ rate_reason: 'before_data_start', data_start: '2026-09-07' }));
		expect(before.querySelector('.status')?.textContent).toContain('7 September 2026');
		expect(before.textContent).not.toContain('31 August');
		unmount(component!); host.remove();
		const changed = render(coverage({ comparable_reason: 'outcome_definition_changed', comparability_before: '2026-09-14' }), null, false);
		expect(changed.querySelector('.comparability')?.textContent).toContain('before 14 September 2026');
		expect(changed.textContent).not.toContain('31 August');
	});

	it('takes the served rate reason as authoritative even when the counts suggest another', () => {
		// No outcomes and no active time: the server says no active time, so the headline does too.
		const idle = render(coverage({ outcomes: 0, sized: 0, estimated_hours: 0, active_hours: 0, unsized: 0, rate_reason: 'no_active_time' }));
		expect(idle.querySelector('.status')?.textContent).toBe('No active time recorded this week, so no rate.');
		unmount(component!); host.remove();
		const unrated = render(coverage({ sized: 0, estimated_hours: 0, rate_reason: 'no_usable_ratings' }));
		expect(unrated.querySelector('.status')?.textContent).toBe('No usable size estimates this week, so no rate.');
		unmount(component!); host.remove();
		const thin = render(coverage({ rate_reason: 'below_minimum' }));
		expect(thin.querySelector('.status')?.textContent).toContain('Too few sized outcomes for a rate; 6.0 estimated hours');
		unmount(component!); host.remove();
		// A reason this client does not know is reported as unavailable, never guessed as too few.
		const future = render(coverage({ rate_reason: 'withheld_for_review' }));
		expect(future.querySelector('.status')?.textContent).toBe('No rate is served for this week.');
		expect(future.textContent).not.toContain('Too few');
	});

	it('shows no status when the latest week has a rate', () => {
		const served = render(coverage({ sized: 24, outcomes: 24, unsized: 0, estimated_hours: 80, active_hours: 0.4 }), 200, true, { latest: win(200, 24), previous: win(null, 0) });
		expect(served.querySelector('.status')).toBeNull();
		expect(served.querySelector('.stat b')?.textContent).toBe('200');
	});

	it('says plainly which outcomes are left out of the hours, and names the comparability reason', () => {
		const period = render(coverage({ outcomes: 1367, sized: 1283, unsized: 80, unknown: 4, estimated_hours: 3600.4, active_hours: 45.5 }), 79.1, true,
			{ latest: win(79.1, 1283, { outcomes: 1367, excluded: 84, estimated_hours: 3600.4, active_hours: 45.5 }), previous: win(null, 0) });
		expect(period.querySelector('.nf')?.textContent).toContain('Hours of work use class midpoints (XS 0.1 h, S 0.5, M 2, L 8, XL 40, XXL 120). 84 of 1,367 accepted outcomes in Sep 7 to Oct 4 have no usable size rating');
		unmount(component!); host.remove();
		const future = render(coverage({ comparable_reason: 'method_retired' }), 10, false);
		expect(future.querySelector('.comparability')?.textContent).toBe('Not comparable with other weeks.');
		unmount(component!); host.remove();
		const known = render(coverage({ comparable_reason: 'mixed_profiles' }), 10, false);
		expect(known.querySelector('.comparability')?.textContent).toContain('rated by more than one rater or rubric');
	});
});
