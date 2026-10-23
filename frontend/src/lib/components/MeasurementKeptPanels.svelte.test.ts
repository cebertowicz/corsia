import { afterEach, describe, expect, it } from 'vitest';
import { mount, unmount } from 'svelte';
import type { MeasurementRow, PanelState, SeriesWindows } from '$lib/stores/measurement';
import MeasurementKeptPanels from './MeasurementKeptPanels.svelte';
import { daysPeriod, periodsPeriod } from '../__tests__/fixtures/measurementPeriod';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

type Series = 'outcomes' | 'took_recommendation_pct' | 'asks_per_outcome' | 'rejections_per_outcome' | 'fresh_tokens_per_outcome';
const VALUES: Record<Series, number> = { outcomes: 40, took_recommendation_pct: 0.6, asks_per_outcome: 0.13, rejections_per_outcome: 0.25, fresh_tokens_per_outcome: 120_000 };
const LAST_DONE = 9; // 2026-10-07; day 10 is today
const day = (i: number) => new Date(Date.parse('2026-09-28T00:00:00Z') + i * 86_400_000).toISOString().slice(0, 10);
const row = (series: Series, period: string, value: number | null, n = 40, partial = false, average: number | null = null, raw: number | null = null): MeasurementRow => ({ period, series, value, n, proxy: false, partial, comparable: true, break: null, dimension: null, thin: false, average, raw_value: raw });
/** What the server pools for the seven days each side of the boundary; the headline reads it and pools nothing itself. */
const POOLED: Record<Series, SeriesWindows> = {
	outcomes: { latest: { value: 280, n: 280 }, previous: { value: 280, n: 280 } },
	took_recommendation_pct: { latest: { value: 0.6, n: 280 }, previous: { value: 0.6, n: 280 } },
	asks_per_outcome: { latest: { value: 0.13, n: 280 }, previous: { value: 0.13, n: 280 } },
	rejections_per_outcome: { latest: { value: 0.25, n: 280 }, previous: { value: 0.25, n: 280 } },
	fresh_tokens_per_outcome: { latest: { value: 120_000, n: 280 }, previous: { value: 120_000, n: 280 } },
};
/** The /series registry minimums and moving-average length, as the server serves them. */
const MINIMUM_N: Record<Series, number> = { outcomes: 0, took_recommendation_pct: 10, asks_per_outcome: 20, rejections_per_outcome: 20, fresh_tokens_per_outcome: 20 };
const state = (rows: MeasurementRow[], period: PanelState['period'], series: Series, windows: Record<Series, SeriesWindows>): PanelState => ({
	loading: false, rows, error: null, selection: 'x', period, windows: { [series]: windows[series] }, averagePeriods: period?.grain === 'days' ? 7 : 4, minimumN: { [series]: MINIMUM_N[series] },
});
const dayRows = (series: Series, extra: MeasurementRow[] = []) => [...Array.from({ length: LAST_DONE + 1 }, (_, i) => row(series, day(i), VALUES[series], 40, false, i >= 6 ? VALUES[series] : null)), ...extra];
const allDays = (): Record<Series, MeasurementRow[]> => ({
	outcomes: dayRows('outcomes'), took_recommendation_pct: dayRows('took_recommendation_pct'), asks_per_outcome: dayRows('asks_per_outcome'),
	rejections_per_outcome: dayRows('rejections_per_outcome'), fresh_tokens_per_outcome: dayRows('fresh_tokens_per_outcome'),
});

/** The Alignment panel loads all three shares: beside each took-recommendation row, Other takes three quarters of the rest and Neither the remainder. */
const withShares = (took: MeasurementRow[]): MeasurementRow[] => took.flatMap((r) => {
	const other = r.value === null ? null : (1 - r.value) * 0.75;
	return [r, { ...r, series: 'answered_other_pct', value: other, average: null, raw_value: null }, { ...r, series: 'answered_neither_pct', value: r.value === null ? null : 1 - r.value - other!, average: null, raw_value: null }];
});

function render(rows: Record<Series, MeasurementRow[]>, period = daysPeriod(day(LAST_DONE)), windows: Record<Series, SeriesWindows> = POOLED, alignmentRows = withShares(rows.took_recommendation_pct)): void {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementKeptPanels, { target: host, props: {
		outcomes: state(rows.outcomes, period, 'outcomes', windows), alignment: state(alignmentRows, period, 'took_recommendation_pct', windows), asks: state(rows.asks_per_outcome, period, 'asks_per_outcome', windows),
		rejections: state(rows.rejections_per_outcome, period, 'rejections_per_outcome', windows), tokens: state(rows.fresh_tokens_per_outcome, period, 'fresh_tokens_per_outcome', windows), onRetry: () => {},
	} });
}
const card = (id: string) => host.querySelector(`#${id}-title`)!.closest('section')!;

describe('kept charts on Days', () => {
	it('calls a no-start response not yet measured, not an empty range', () => {
		const empty = { outcomes: [], took_recommendation_pct: [], asks_per_outcome: [], rejections_per_outcome: [], fresh_tokens_per_outcome: [] };
		render(empty);
		for (const id of ['outcomes', 'alignment', 'asks', 'rejections', 'tokens']) {
			expect(card(id).textContent).toContain('Not yet measured for this scope.');
			expect(card(id).textContent).not.toContain('No measurement rows in this range.');
		}
	});

	for (const [id, label] of [['outcomes', 'Outcomes'], ['asks', 'Asks'], ['rejections', 'Rejections'], ['tokens', 'Fresh tokens']] as const) {
		it(`${label} reads BY DAY with a bar per day and the 7-day moving average`, () => {
			render(allDays());
			const section = card(id);
			expect(section.querySelector('h4')?.textContent).toMatch(/, by day$/);
			expect(section.querySelectorAll('.dsk svg rect.bar')).toHaveLength(LAST_DONE + 1);
			// The line starts on the seventh day, where the server first serves an average.
			expect(section.querySelector('.dsk svg polyline.ma')?.getAttribute('points')?.split(' ')).toHaveLength(LAST_DONE - 5);
			expect(section.querySelector('.lg')?.textContent).toContain('One day');
			expect(section.querySelector('.lg')?.textContent).toContain('7-day moving average');
			expect(section.querySelector('figcaption')?.textContent).not.toContain('per week');
			expect(section.querySelector('figcaption')?.textContent).not.toContain('moving average');
			expect(section.querySelector('figcaption')?.textContent).not.toContain('no moving average');
		});
	}

	it('draws a thin day hollow on a rate chart but never on Outcomes, whose n is its value', () => {
		const rows = allDays();
		// Day 3 has five outcomes; the server withholds each value below its minimum and serves the raw one.
		const thin = (series: Series, raw: number) => dayRows(series).map((r) => (r.period === day(3) ? { ...r, value: null, n: 5, raw_value: raw } : r));
		rows.outcomes = thin('outcomes', 5);
		rows.asks_per_outcome = thin('asks_per_outcome', 0.13);
		render(rows);
		const outcomes = card('outcomes');
		expect(outcomes.querySelectorAll('.dsk svg rect.bar')).toHaveLength(LAST_DONE + 1);
		expect(outcomes.querySelectorAll('.dsk svg rect.bar.low')).toHaveLength(0);
		expect(outcomes.querySelector('.dsk svg text.lo')).toBeNull();
		expect(outcomes.querySelector('.lg')?.textContent).not.toContain('Hollow bar');
		const asks = card('asks');
		expect(asks.querySelectorAll('.dsk svg rect.bar.low')).toHaveLength(1);
		expect([...asks.querySelectorAll('.dsk svg text.lo')].map((n) => n.textContent)).toEqual(['low n']);
		expect(asks.querySelector('.lg')?.textContent).toContain('Hollow bar: fewer than 20 outcomes (low n)');
	});

	it('ends the series at its last day with a value, drawing no trailing empty days', () => {
		const rows = allDays();
		rows.outcomes = dayRows('outcomes', [row('outcomes', day(10), null, 0), row('outcomes', day(11), null, 0)]);
		render(rows);
		const section = card('outcomes');
		expect(section.querySelectorAll('.dsk svg rect.bar')).toHaveLength(LAST_DONE + 1);
		expect(section.querySelector('.dsk svg text.lo')).toBeNull();
	});

	it('keeps a day whose share the server withheld but that has answers behind it, labelled too few with its n, and never calls it no data', () => {
		const rows = allDays();
		// Day 3 answered 5 decisions (withheld below the server minimum); the last two days answered 8 and 6; day 10 and 11 answered none.
		rows.took_recommendation_pct = dayRows('took_recommendation_pct').map((r) => (r.period === day(3) ? { ...r, value: null, n: 5 } : r.period === day(8) ? { ...r, value: null, n: 8 } : r.period === day(9) ? { ...r, value: null, n: 6 } : r))
			.concat([row('took_recommendation_pct', day(10), null, 0), row('took_recommendation_pct', day(11), null, 0)]);
		render(rows);
		const svg = card('alignment').querySelector('.mob svg')!;
		// Days 3, 8 and 9 draw no stack yet stay on the chart: the trailing withheld days are not "empty", the zero-answer days are.
		expect(svg.querySelectorAll('rect.stack')).toHaveLength((LAST_DONE + 1 - 3) * 3);
		expect([...svg.querySelectorAll('text.nn')].map((n) => n.textContent)).toEqual(['n', ...Array.from({ length: LAST_DONE + 1 }, (_, i) => (i === 3 ? '5' : i === 8 ? '8' : i === 9 ? '6' : '40'))]);
		expect([...svg.querySelectorAll('text.lo')].map((n) => n.textContent)).toEqual(['too few', 'too few', 'too few']);
		expect(card('alignment').textContent).not.toContain('no data');
		expect(card('alignment').querySelector('.lg')?.textContent).toContain('too few');
	});

	it('outlines the running day on Alignment, labels it today so far and never reads it as a finished day', () => {
		const rows = allDays();
		rows.took_recommendation_pct = dayRows('took_recommendation_pct', [row('took_recommendation_pct', day(10), 0.5, 12, true)]);
		render(rows);
		const section = card('alignment');
		const svg = section.querySelector('.dsk svg')!;
		expect(svg.querySelectorAll('rect.stack')).toHaveLength((LAST_DONE + 2) * 3);
		expect(svg.querySelectorAll('rect.stack.part')).toHaveLength(3);
		expect([...svg.querySelectorAll('text.lo')].map((n) => n.textContent)).toEqual(['today so far']);
		expect(section.querySelector('.lg')?.textContent).toContain('Today so far: the day is still running');
		const rowsText = [...section.querySelectorAll('table.sr-only tbody tr')].map((n) => n.textContent);
		expect(rowsText.at(-1)).toContain('today so far');
		expect(rowsText.at(-2)).not.toContain('today so far');
	});

	it('reads Alignment by its served minimum of 10 decisions, in the chart and the change, never the outcome rates\' 20', () => {
		const rows = allDays();
		// Twelve decisions a day, and day 3 with eight, which the server withheld under its 10 and served raw.
		rows.took_recommendation_pct = dayRows('took_recommendation_pct').map((r) => (r.period === day(3) ? { ...r, value: null, n: 8, raw_value: r.value } : { ...r, n: 12 }));
		render(rows, daysPeriod(day(LAST_DONE)), { ...POOLED, took_recommendation_pct: { latest: { value: 0.6, n: 12 }, previous: { value: 0.5, n: 12 } } });
		const alignment = card('alignment');
		// Twelve decisions clear the served 10: only day 3, withheld, draws no stack, and the change reads past low n (new: the series began inside the previous window).
		expect(alignment.querySelectorAll('.dsk svg rect.stack')).toHaveLength(LAST_DONE * 3);
		expect([...alignment.querySelectorAll('.dsk svg text.lo')].map((n) => n.textContent)).toEqual(['too few']);
		expect(alignment.querySelector('.stat')?.textContent).toContain('n 12, new');
		expect(alignment.querySelector('.stat')?.textContent).not.toContain('low n');
		expect(alignment.textContent).not.toContain('fewer than 20');
	});

	for (const [id, series] of [['rejections', 'rejections_per_outcome'], ['tokens', 'fresh_tokens_per_outcome']] as const) {
		it(`${id}: a low-n day the server withheld draws hollow with its raw value and low n, and the axis follows the full bars`, () => {
			const rows = allDays();
			const full = Math.max(...dayRows(series).map((r) => r.value as number));
			rows[series] = dayRows(series).map((r) => (r.period === day(3) ? { ...r, value: null, n: 5, raw_value: full * 0.5 } : r));
			render(rows);
			const svg = card(id).querySelector('.dsk svg')!;
			const hollow = svg.querySelectorAll('rect.bar.low');
			expect(hollow).toHaveLength(1);
			expect(svg.querySelectorAll('rect.bar')).toHaveLength(LAST_DONE + 1);
			expect([...svg.querySelectorAll('text.lo')].map((n) => n.textContent)).toEqual(['low n']);
			expect(svg.querySelectorAll('text.vl')).toHaveLength(LAST_DONE + 1);
			expect(card(id).querySelector('.lg')?.textContent).toContain('Hollow bar');
			expect(card(id).querySelector('.lg')?.textContent).not.toContain('too few');
			expect(card(id).querySelector('table.sr-only tbody tr:nth-child(4)')?.textContent).toContain(`fewer than ${MINIMUM_N[series]}`);
			// The ticks are the same as the chart without the hollow day.
			const ticks = (root: Element) => [...root.querySelectorAll('text.tk')].map((n) => n.textContent);
			const withHollow = ticks(svg);
			unmount(component!); host.remove();
			render(allDays());
			expect(ticks(card(id).querySelector('.dsk svg')!)).toEqual(withHollow);
		});
	}

	it('shows a day with no outcomes the same way on every kept chart: no bar, n 0, no label', () => {
		const rows = allDays();
		for (const series of Object.keys(rows) as Series[]) rows[series] = dayRows(series).map((r) => (r.period === day(4) ? { ...r, value: null, n: 0 } : r));
		render(rows);
		for (const id of ['outcomes', 'alignment', 'asks', 'rejections', 'tokens']) {
			const svg = card(id).querySelector('.dsk svg')!;
			expect(svg.querySelectorAll(id === 'alignment' ? 'rect.stack' : 'rect.bar')).toHaveLength(id === 'alignment' ? LAST_DONE * 3 : LAST_DONE);
			expect([...svg.querySelectorAll('text.nn')].map((n) => n.textContent)[5]).toBe('0');
			expect(svg.querySelectorAll('text.lo')).toHaveLength(0);
			expect(card(id).textContent).not.toContain('no data');
		}
	});

	it('draws today as a partial bar for Outcomes and Fresh tokens: outlined, labelled today so far, not the latest, and out of the headline', () => {
		const rows = allDays();
		rows.outcomes = dayRows('outcomes', [row('outcomes', day(10), 7, 7, true)]);
		rows.fresh_tokens_per_outcome = dayRows('fresh_tokens_per_outcome', [row('fresh_tokens_per_outcome', day(10), 50_000, 7, true)]);
		render(rows);
		for (const id of ['outcomes', 'tokens']) {
			const svg = card(id).querySelector('.dsk svg')!;
			const bars = [...svg.querySelectorAll('rect.bar')];
			expect(bars).toHaveLength(LAST_DONE + 2);
			expect(bars[bars.length - 1].classList.contains('part')).toBe(true);
			expect(bars[bars.length - 1].classList.contains('cur')).toBe(false);
			expect(bars[bars.length - 2].classList.contains('cur')).toBe(true);
			expect([...svg.querySelectorAll('text.lo')].map((n) => n.textContent)).toEqual(['today so far']);
			expect(card(id).querySelector('.lg')?.textContent).toContain('Today so far');
			expect(card(id).querySelector('table.sr-only tbody tr:last-child')?.textContent).toContain('today so far');
		}
		// The headline pools the seven completed days the period names (7 x 40); today's 7 stay out of it.
		expect(card('outcomes').querySelector('.stat')?.textContent).toContain('280 accepted outcomes');
		expect(card('outcomes').querySelector('.stat')?.textContent).toContain('n 280');
		expect(card('tokens').querySelector('.stat')?.textContent).toContain('120k fresh tokens per outcome');
	});

	it('draws Asks per outcome as one all-agents bar card after Outcomes, its bars, labels and read-out in the same two-decimal figures', () => {
		const rows = allDays();
		rows.asks_per_outcome = dayRows('asks_per_outcome').map((r, i) => ({ ...r, value: 0.1 + i / 100 }));
		render(rows);
		const order = [...host.querySelectorAll('section.card h3')].map((h) => h.textContent);
		expect(order).toEqual(['Outcomes', 'Asks per outcome', 'Alignment', 'Rejections per outcome', 'Fresh tokens per outcome']);
		const section = card('asks');
		expect(section.classList.contains('card')).toBe(true);
		expect(section.querySelector('.stat')?.textContent).toContain('0.13 asks per outcome');
		expect(section.querySelector('h4')?.textContent).toMatch(/^Asks of you per accepted outcome, .+, by day$/);
		const svg = section.querySelector('.dsk svg')!;
		// One series: one bar per day, none split by agent.
		expect(svg.querySelectorAll('rect.bar')).toHaveLength(LAST_DONE + 1);
		const shown = [...svg.querySelectorAll('text.vl')].map((n) => n.textContent);
		expect(shown).toEqual(Array.from({ length: LAST_DONE + 1 }, (_, i) => (0.1 + i / 100).toFixed(2)));
		// The screen-reader table reads each day in the same formatter the bar labels use.
		const read = [...section.querySelectorAll('table.sr-only tbody tr td:first-of-type')].map((n) => n.textContent);
		expect(read).toEqual(shown);
	});

	it('draws no bar for a per-outcome value today when no outcome stands behind it', () => {
		const rows = allDays();
		rows.fresh_tokens_per_outcome = dayRows('fresh_tokens_per_outcome', [row('fresh_tokens_per_outcome', day(10), null, 0, true)]);
		render(rows);
		const svg = card('tokens').querySelector('.dsk svg')!;
		expect(svg.querySelectorAll('rect.bar')).toHaveLength(LAST_DONE + 1);
		expect(svg.querySelector('rect.part')).toBeNull();
	});
});

describe('kept chart headlines', () => {
	// Each chart has a withheld day (value null, n above 0) inside the latest window; the server's pooled figure still counts it.
	const SERVED: Record<Series, SeriesWindows> = {
		outcomes: { latest: { value: 495, n: 495 }, previous: { value: 1131, n: 1131 } },
		took_recommendation_pct: { latest: { value: 0.55, n: 40 }, previous: { value: 0.5317, n: 126 } },
		asks_per_outcome: { latest: { value: 0.13, n: 495 }, previous: { value: 0.1, n: 1131 } },
		rejections_per_outcome: { latest: { value: 0.18, n: 495 }, previous: { value: 0.12, n: 1131 } },
		fresh_tokens_per_outcome: { latest: { value: 1_057_000, n: 495 }, previous: { value: 734_000, n: 1131 } },
	};
	// The earlier days make the previous window fully covered, so the change reads as a percentage and not as new.
	const withheld = (series: Series) => [...[-4, -3, -2, -1].map((i) => row(series, day(i), VALUES[series])), ...dayRows(series)].map((r) => (r.period === day(8) ? { ...r, value: null, n: 8 } : r));
	const grouped = { outcomes: ['outcomes', '495 accepted outcomes', 'n 495', '-56% vs'], alignment: ['took_recommendation_pct', '55% took', 'n 40', 'n 126'], asks: ['asks_per_outcome', '0.13 asks per outcome', 'n 495', '+30% vs'], rejections: ['rejections_per_outcome', '0.18 rejections', 'n 495', '+50% vs'], tokens: ['fresh_tokens_per_outcome', '1,057k fresh tokens', 'n 495', '+44% vs'] } as const;
	for (const [id, [series, value, n, change]] of Object.entries(grouped)) {
		it(`${id} headline is the served window, with a withheld day inside it`, () => {
			const rows = allDays();
			rows[series as Series] = withheld(series as Series);
			render(rows, daysPeriod(day(LAST_DONE)), SERVED);
			const stat = card(id).querySelector('.stat')!.textContent!;
			expect(stat).toContain(value);
			expect(stat).toContain(n);
			expect(stat).toContain(change);
			expect(stat).not.toContain('low n');
			expect(card(id).querySelector('.dsk svg')!.querySelectorAll('text.lo')).toHaveLength(1);
		});
	}
});

describe('kept charts on Weeks', () => {
	it('keep the 4-week moving average and never draw a running week', () => {
		const wk = (i: number) => new Date(Date.parse('2026-08-10T00:00:00Z') + i * 7 * 86_400_000).toISOString().slice(0, 10);
		const make = (series: Series) => [...Array.from({ length: 8 }, (_, i) => row(series, wk(i), VALUES[series], 40, false, i >= 3 ? VALUES[series] : null)), row(series, wk(8), 3, 3, true)];
		render({ outcomes: make('outcomes'), took_recommendation_pct: make('took_recommendation_pct'), asks_per_outcome: make('asks_per_outcome'), rejections_per_outcome: make('rejections_per_outcome'), fresh_tokens_per_outcome: make('fresh_tokens_per_outcome') }, periodsPeriod(wk(7)));
		const section = card('outcomes');
		expect(section.querySelector('h4')?.textContent).toMatch(/, by week$/);
		expect(section.querySelectorAll('.dsk svg rect.bar')).toHaveLength(8);
		expect(section.querySelector('.dsk svg polyline.ma')?.getAttribute('points')?.split(' ')).toHaveLength(5);
		expect(section.querySelector('.lg')?.textContent).toContain('4-week moving average');
		expect(section.querySelector('figcaption')?.textContent).not.toContain('moving average');
	});
});

describe('the caption names the line only when the chart draws it', () => {
	const wk = (i: number) => new Date(Date.parse('2026-09-14T00:00:00Z') + i * 7 * 86_400_000).toISOString().slice(0, 10);
	const periods = (average: number | null) => (series: Series) => [0, 1, 2].map((i) => row(series, wk(i), VALUES[series], 40, false, i === 2 ? average : null));
	const all = (make: (series: Series) => MeasurementRow[]) => ({
		outcomes: make('outcomes'), took_recommendation_pct: make('took_recommendation_pct'), asks_per_outcome: make('asks_per_outcome'),
		rejections_per_outcome: make('rejections_per_outcome'), fresh_tokens_per_outcome: make('fresh_tokens_per_outcome'),
	});
	for (const id of ['outcomes', 'alignment', 'asks', 'rejections', 'tokens']) {
		it(`${id}: three weeks with no moving average name no line in the caption or the legend`, () => {
			render(all(periods(null)), periodsPeriod(wk(2)));
			const caption = card(id).querySelector('figcaption')!.textContent!;
			expect(caption).not.toContain('moving average');
			expect(card(id).querySelector('.lg')?.textContent).not.toContain('moving average');
			expect(card(id).querySelector('svg polyline.ma')).toBeNull();
		});
		it(`${id}: a served moving average keeps the legend entry and leaves the caption without a line sentence${id === 'alignment' ? ' only where a line is drawn' : ''}`, () => {
			render(all((series) => periods(VALUES[series])(series)), periodsPeriod(wk(2)));
			if (id === 'alignment') {
				// The shares chart draws no line, so its caption never names one.
				expect(card(id).querySelector('figcaption')?.textContent).not.toContain('moving average');
				return;
			}
			expect(card(id).querySelector('figcaption')?.textContent).not.toContain('moving average');
			expect(card(id).querySelector('.lg')?.textContent).toContain('4-week moving average');
		});
	}
});

describe('Alignment draws its three shares as one stacked bar per period, as before the parity pass', () => {
	const wk = (i: number) => new Date(Date.parse('2026-08-10T00:00:00Z') + i * 7 * 86_400_000).toISOString().slice(0, 10);
	const weeks = (series: Series) => Array.from({ length: 8 }, (_, i) => row(series, wk(i), VALUES[series], 40));
	const all = (): Record<Series, MeasurementRow[]> => ({ outcomes: weeks('outcomes'), took_recommendation_pct: weeks('took_recommendation_pct'), asks_per_outcome: weeks('asks_per_outcome'), rejections_per_outcome: weeks('rejections_per_outcome'), fresh_tokens_per_outcome: weeks('fresh_tokens_per_outcome') });

	for (const view of ['dsk', 'mob']) {
		it(`${view}: took, Other and Neither stack bottom to top to 100% on 0, 50 and 100% ticks, with an n under each week`, () => {
			render(all(), periodsPeriod(wk(7)));
			const svg = card('alignment').querySelector(`.${view} svg`)!;
			expect(svg.querySelectorAll('rect.bar')).toHaveLength(0);
			const stacks = [...svg.querySelectorAll('rect.stack')];
			expect(stacks).toHaveLength(8 * 3);
			expect(stacks.slice(0, 3).map((r) => ['s0', 's1', 's2'].find((c) => r.classList.contains(c)))).toEqual(['s0', 's1', 's2']);
			// Bottom to top: took (60%) sits on the axis, Other (30%) on it, Neither (10%) on top.
			const [took, other, neither] = stacks.slice(0, 3).map((r) => ({ y: Number(r.getAttribute('y')), h: Number(r.getAttribute('height')) }));
			expect(other.y + other.h).toBeCloseTo(took.y, 0);
			expect(neither.y + neither.h).toBeCloseTo(other.y, 0);
			expect(took.h / (took.h + other.h + neither.h)).toBeCloseTo(0.6, 2);
			expect([...svg.querySelectorAll('text.tk')].map((n) => n.textContent)).toEqual(['0%', '50%', '100%']);
			expect([...svg.querySelectorAll('text.nn')].map((n) => n.textContent)).toEqual(['n', ...Array(8).fill('40')]);
			expect(svg.querySelector('polyline.ma')).toBeNull();
		});
	}

	it('writes each share on its segment where the bar has room, and names the three in the legend and the table', () => {
		render(all(), periodsPeriod(wk(7)));
		const section = card('alignment');
		expect([...section.querySelectorAll('.lg span')].map((n) => n.textContent)).toEqual(['Took recommendation', 'Answered Other', 'Neither']);
		// Desktop bars are wide enough for a label; Neither's 10% segment is too short for one.
		expect([...section.querySelectorAll('.dsk svg text.sl')].slice(0, 2).map((n) => n.textContent)).toEqual(['60%', '30%']);
		expect(section.querySelectorAll('.dsk svg text.sl.s2')).toHaveLength(0);
		expect([...section.querySelectorAll('table.sr-only thead th')].map((n) => n.textContent)).toEqual(['Week starting', 'Took recommendation', 'Answered Other', 'Neither', 'n']);
		expect(section.querySelector('table.sr-only tbody tr')?.textContent).toBe('2026-08-1060%30%10%40');
	});

	it('holds a place for each week before Alignment started on the shared eight-week axis, drawing no stack and no n there', () => {
		const rows = all();
		// Alignment's data begins at the fifth of the eight weeks; the other series cover all of them.
		const late = withShares(rows.took_recommendation_pct.filter((r) => r.period >= wk(4)));
		render(rows, periodsPeriod(wk(7)), POOLED, late);
		const svg = card('alignment').querySelector('.dsk svg')!;
		expect(svg.querySelectorAll('rect.stack').length).toBe(4 * 3);
		expect([...svg.querySelectorAll('text.nn')].map((n) => n.textContent)).toEqual(['n', '40', '40', '40', '40']);
		// The first drawn stack sits in the fifth slot of eight, not the first.
		const slot = (Number(svg.querySelector('line.zero')!.getAttribute('x2')) - Number(svg.querySelector('line.zero')!.getAttribute('x1'))) / 8;
		expect(Number(svg.querySelector('rect.stack')!.getAttribute('x'))).toBeGreaterThan(Number(svg.querySelector('line.zero')!.getAttribute('x1')) + slot * 4);
		expect(card('alignment').textContent).not.toContain('not recorded');
	});

	it('draws no stack for a week whose shares do not add up to the answered decisions, and says why in the table', () => {
		const rows = all();
		const shares = withShares(rows.took_recommendation_pct).map((r) => (r.period === wk(2) && r.series === 'answered_neither_pct' ? { ...r, value: 0.5 } : r));
		render(rows, periodsPeriod(wk(7)), POOLED, shares);
		const section = card('alignment');
		expect(section.querySelectorAll('.dsk svg rect.stack')).toHaveLength(7 * 3);
		expect(section.querySelectorAll('table.sr-only tbody tr')[2].textContent).toContain('do not add up');
		// Plenty of decisions were answered that week, so the chart must not call it too few.
		expect(section.querySelectorAll('.dsk svg text.lo')).toHaveLength(0);
		expect(section.querySelector('.lg')?.textContent).not.toContain('too few');
	});
});
