import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MeasurementOutcomeRates from './MeasurementOutcomeRates.svelte';
import type { MeasurementRow, PanelState } from '$lib/stores/measurement';
import { measurementAgents, type MeasurementAgentRow } from '$lib/stores/measurementAgents';
import { periodsPeriod } from '../__tests__/fixtures/measurementPeriod';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;
const noAgents = { loading: false, error: null, rows: [], findings: [], findingsState: null, caption: '', selection: '', scopeKey: '', servedView: null, grain: null, sort: null, projectSummaries: [], inactiveProjects: [], summariesKey: null };
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); measurementAgents.set(noAgents); });

const periodAt = (i: number) => new Date(Date.parse('2026-09-07T00:00:00Z') + i * 7 * 86_400_000).toISOString().slice(0, 10);
const rows = (series: string, values: (number | null)[], n = 40): MeasurementRow[] => values.map((value, i) => ({
	period: periodAt(i), series, value, n, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false,
}));
const panel = (state: Partial<PanelState> = {}): PanelState => ({
	loading: false, error: null, rows: [], selection: 'weeks:all:none', ...(state.rows?.length ? { period: periodsPeriod(state.rows[state.rows.length - 1].period) } : {}), ...state,
});
/** The served pooled figure for the latest and previous window; the headline reads it and pools nothing itself. */
const served = (series: string, latest: [number | null, number], previous: [number | null, number]) => ({ [series]: { latest: { value: latest[0], n: latest[1] }, previous: { value: previous[0], n: previous[1] } } });
const text = (el: Element | null | undefined) => el?.textContent?.replace(/\s+/g, ' ').trim() ?? '';

function render(attention: PanelState, asks: PanelState, firstTry: PanelState, onRetry = (_: string) => {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementOutcomeRates, { target: host, props: { attention, asks, firstTry, onRetry } });
	return host;
}

describe('MeasurementOutcomeRates', () => {
	const eight = (a: number, b: number) => [a, a, a, a, b, b, b, b];

	it('draws minutes per outcome and first try by week, with asks as one line under the minutes', () => {
		render(
			panel({ rows: rows('active_hours_per_outcome', eight(0.1, 0.05)), windows: served('active_hours_per_outcome', [0.05, 160], [0.1, 160]) }),
			panel({ rows: rows('asks_per_outcome', eight(0.2, 0.1)), windows: served('asks_per_outcome', [0.1, 160], [0.2, 160]) }),
			panel({ rows: rows('first_try_acceptance', eight(0.8, 0.9)), windows: served('first_try_acceptance', [0.9, 160], [0.8, 160]) }),
		);
		const [minutes, firstTry] = [...host.querySelectorAll('section.card')];
		expect(text(minutes.querySelector('h3'))).toBe('Your minutes per outcome');
		expect(text(minutes.querySelector('.stat'))).toBe('3.0 minutes per outcome, Oct 5 to Nov 1, n 160, -50% vs Sep 7 to Oct 4 (n 160)');
		expect(text(minutes.querySelector('.asks'))).toBe('All agents together: 0.10 decisions and tasks per outcome, -50%.');
		expect(text(minutes.querySelector('h4'))).toBe('Minutes of your engagement per outcome, Sep 7 to Nov 1, by week');
		expect(text(minutes.querySelector('figcaption'))).toContain('Your engaged minutes divided by accepted outcomes; source: your time active in CORSIA and accepted outcomes; lower is better. n is accepted outcomes per week.');
		expect(minutes.querySelectorAll('svg .bar').length).toBeGreaterThan(0);
		expect(text(firstTry.querySelector('h3'))).toBe('First-try rate (agent-graded)');
		expect(text(firstTry.querySelector('.stat'))).toBe('90% first try, Oct 5 to Nov 1, n 160, +13% vs Sep 7 to Oct 4 (n 160)');
		expect(text(firstTry.querySelector('figcaption'))).toContain('graded by the agents; source: agent grades; higher is better.');
		expect(host.querySelectorAll('#asks-title')).toHaveLength(0);
		expect(host.textContent).not.toMatch(/Decisions and tasks for you per outcome/);
	});

	it('puts the asks line under the agents table, not under the headline', () => {
		const agent = { agent: { id: 'a1', name: 'Agent a1', project: { id: 'p1', name: 'CORSIA' } }, n: 30, active_minutes: 154, difficulty_mix: {}, status: 'eligible', rank: 1, selected_metric: 'fresh_tokens_per_outcome',
			metrics: { active_hours_per_outcome: { value: 0.1, n: 40, minimum_n: 20, periods: [] } }, worsening: { eligible: false, metric: 'm', minimum_n: 20 }, flags: {} } as unknown as MeasurementAgentRow;
		measurementAgents.set({ ...noAgents, rows: [agent] });
		render(
			panel({ rows: rows('active_hours_per_outcome', eight(0.1, 0.05)), windows: served('active_hours_per_outcome', [0.05, 160], [0.1, 160]) }),
			panel({ rows: rows('asks_per_outcome', eight(0.2, 0.1)), windows: served('asks_per_outcome', [0.1, 160], [0.2, 160]) }),
			panel({ rows: rows('first_try_acceptance', eight(0.8, 0.9)), windows: served('first_try_acceptance', [0.9, 160], [0.8, 160]) }),
		);
		flushSync();
		const minutes = host.querySelector('section.card')!;
		const table = minutes.querySelector('table[aria-labelledby="agents-minutes-title"]')!;
		const asks = minutes.querySelector('.asks')!;
		expect(table).not.toBeNull();
		expect(table.compareDocumentPosition(asks) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(table.nextElementSibling).toBe(asks);
		expect(minutes.querySelector('.stat')!.nextElementSibling?.tagName).toBe('H4');
	});

	it('draws a low-n week of Minutes and First try hollow with its raw value, in the reader\'s unit, labelled low n', () => {
		const lowPeriod = (series: string, values: (number | null)[], raw: number): MeasurementRow[] => rows(series, values).map((r, i) => (i === 2 ? { ...r, value: null, n: 6, raw_value: raw } : r));
		render(
			panel({ rows: lowPeriod('active_hours_per_outcome', eight(0.1, 0.05), 0.08), windows: served('active_hours_per_outcome', [0.05, 160], [0.1, 160]) }),
			panel({ rows: rows('asks_per_outcome', eight(0.2, 0.1)), windows: served('asks_per_outcome', [0.1, 160], [0.2, 160]) }),
			panel({ rows: lowPeriod('first_try_acceptance', eight(0.8, 0.9), 0.5), windows: served('first_try_acceptance', [0.9, 160], [0.8, 160]) }),
		);
		const [minutes, firstTry] = [...host.querySelectorAll('section.card')];
		for (const card of [minutes, firstTry]) {
			expect(card.querySelectorAll('.mob svg rect.bar.low')).toHaveLength(1);
			expect([...card.querySelectorAll('.mob svg text.lo')].map((n) => n.textContent)).toEqual(['low n']);
			expect(card.querySelector('.lg')?.textContent).not.toContain('too few');
		}
		// 0.08 hours is 4.8 minutes: the raw value is shown in the unit the chart reads in.
		expect([...minutes.querySelectorAll('.mob svg text.vl')].map((n) => n.textContent)[2]).toBe('4.8');
		expect([...firstTry.querySelectorAll('.mob svg text.vl')].map((n) => n.textContent)[2]).toBe('50');
	});

	it('shows unknown as a dash, never zero, when every week is withheld or too small', () => {
		render(
			panel({ rows: rows('active_hours_per_outcome', [0.1, 0.1, null, null], 8), windows: served('active_hours_per_outcome', [0.1, 16], [null, 0]) }),
			panel({ rows: rows('asks_per_outcome', [null], 3), windows: served('asks_per_outcome', [null, 3], [null, 0]) }),
			panel({ rows: rows('first_try_acceptance', [null, null], 3) }),
		);
		const [minutes, firstTry] = [...host.querySelectorAll('section.card')];
		expect(text(minutes.querySelector('.stat'))).toContain('6.0 minutes per outcome');
		expect(text(minutes.querySelector('.stat'))).toContain('low n');
		expect(text(minutes.querySelector('.asks'))).toBe('All agents together: – decisions and tasks per outcome, n/a.');
		expect(text(firstTry)).toContain('Nothing was measured for this selection.');
		expect(firstTry.querySelector('svg')).toBeNull();
	});

	it('keeps loading, empty and error states per card and retries the failed panel', () => {
		const retried: string[] = [];
		render(panel({ loading: true }), panel({ error: 'boom' }), panel({ error: 'boom' }), (id) => retried.push(id));
		const [minutes, firstTry] = [...host.querySelectorAll('section.card')];
		expect(minutes.querySelector('[aria-label="Loading Your minutes per outcome"]')).not.toBeNull();
		expect(text(minutes.querySelector('.asks'))).toBe('');
		(firstTry.querySelector('.panel-error button') as HTMLButtonElement).click();
		expect(retried).toEqual(['first-try']);
	});
});
