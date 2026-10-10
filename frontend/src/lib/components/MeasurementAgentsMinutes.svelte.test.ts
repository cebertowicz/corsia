import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MeasurementAgentsMinutes from './MeasurementAgentsMinutes.svelte';
import minutesSource from './MeasurementAgentsMinutes.svelte?raw';
import agentsSource from './MeasurementAgentsTable.svelte?raw';
import changesSource from './MeasurementWhereToLook.svelte?raw';
import type { AgentsState, MeasurementAgentRow } from '$lib/stores/measurementAgents';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

const period = {
	grain: 'days' as const, timezone: 'America/Los_Angeles', current: '2026-10-05',
	range: { from: '2026-09-22', to: '2026-10-04' },
	latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' },
};

/** A row whose minutes per outcome is `minutes`, or unmeasured when null. */
function row(id: string, minutes: number | null, over: Partial<MeasurementAgentRow> = {}): MeasurementAgentRow {
	const cell = { value: minutes === null ? null : minutes / 60, n: 40, minimum_n: 20, periods: [] };
	return {
		agent: { id, name: `Agent ${id}`, project: { id: 'p1', name: 'CORSIA' } },
		n: 30, active_minutes: 154, difficulty_mix: {}, status: 'eligible', rank: 1, selected_metric: 'fresh_tokens_per_outcome',
		metrics: { active_hours_per_outcome: cell }, worsening: { eligible: false, metric: 'm', minimum_n: 20 }, flags: {},
		...over,
	} as MeasurementAgentRow;
}

function render(rows: MeasurementAgentRow[], over: Partial<AgentsState> = {}): void {
	const state: AgentsState = {
		loading: false, error: null, rows, scopeKey: 'all:none', servedView: 'v', findings: [], findingsState: 'healthy',
		caption: '', selection: 's', grain: 'days', sort: 'ranked', period, projectSummaries: [], inactiveProjects: [], summariesKey: 'k',
		...over,
	};
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementAgentsMinutes, { target: host, props: { state } });
	flushSync();
}

describe('MeasurementAgentsMinutes', () => {
	it('names the latest window and shows the approved columns', () => {
		render([row('a', 5.1)]);
		expect(host.querySelector('h4')?.textContent).toBe('Agents with the most of your minutes per outcome, Sep 28 to Oct 4');
		expect([...host.querySelectorAll('thead th')].map((cell) => cell.textContent)).toEqual(['Agent', 'Minutes per outcome', 'Your minutes', 'Outcomes']);
	});

	it('lists the five costliest agents, costliest first, and leaves out unmeasured ones', () => {
		render([row('a', 1), row('b', 4.7), row('c', null), row('d', 5.1), row('e', 2.4), row('f', 4.4), row('g', 3)]);
		const names = [...host.querySelectorAll('tbody th')].map((cell) => cell.textContent);
		expect(names).toEqual(['Agent d', 'Agent b', 'Agent f', 'Agent g', 'Agent e']);
		const first = [...host.querySelectorAll('tbody tr:first-child td')].map((cell) => cell.textContent);
		expect(first).toEqual(['5.1', '154', '30']);
	});

	it('shows one decimal under 10 and whole numbers from 10', () => {
		render([row('a', 3.64, { active_minutes: 7.25 }), row('b', 12.6, { active_minutes: 134.6 })]);
		const cells = [...host.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((cell) => cell.textContent));
		expect(cells).toEqual([['13', '135', '30'], ['3.6', '7.3', '30']]);
	});

	it('capitalises the first letter of an agent name', () => {
		render([row('a', 5.1, { agent: { id: 'a', name: 'conductor', project: { id: 'p1', name: 'CORSIA' } } })]);
		expect(host.querySelector('tbody th')?.textContent).toBe('Conductor');
	});

	it('shows a dash for minutes the server did not serve', () => {
		render([row('a', 5.1, { active_minutes: null })]);
		expect(host.querySelectorAll('tbody tr:first-child td')[1].textContent).toContain('–');
	});

	it('renders nothing when no agent has a measured value', () => {
		render([row('a', null)]);
		expect(host.querySelector('table')).toBeNull();
		expect(host.querySelector('h4')).toBeNull();
	});
});

/**
 * Every rule in a component's style block that lets a word break anywhere, as [selector, declarations].
 * overflow-wrap is inherited, so a rule on the table or its head reaches the header cells too.
 */
function midWordRules(source: string): [string, string][] {
	const css = source.slice(source.indexOf('<style>') + '<style>'.length).replace(/\/\*[\s\S]*?\*\//g, '');
	return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
		.filter(([, , body]) => /overflow-wrap:\s*(anywhere|break-word)|word-break:\s*(break-all|break-word)/.test(body))
		.map(([, selector, body]) => [selector.trim(), body.trim()]);
}

describe('measurement table headers', () => {
	it('never break a header word mid-word, in the Minutes, Agents or Biggest changes table', () => {
		for (const [name, source] of [['Minutes', minutesSource], ['Agents', agentsSource], ['Biggest changes', changesSource]]) {
			const reachesHeader = midWordRules(source).flatMap(([selector]) => selector.split(','))
				.map((part) => part.trim())
				.filter((part) => !/\btbody\b/.test(part) && /(^|[\s>])(th|thead|table|tr)\b|\.tb$/.test(part));
			expect(reachesHeader, `${name} table: no break-anywhere rule reaches a header cell`).toEqual([]);
		}
	});

	it('wraps agent names and subjects between words only, never mid-word, in the Minutes and Biggest changes tables', () => {
		expect(midWordRules(minutesSource)).toEqual([]);
		const subjectRules = midWordRules(changesSource).flatMap(([selector]) => selector.split(',')).map((part) => part.trim());
		expect(subjectRules.filter((part) => /first-child|\bth\b|\btd\b/.test(part))).toEqual([]);
	});
});
