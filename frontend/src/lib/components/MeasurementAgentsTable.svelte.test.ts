import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { flushSync, mount, unmount } from 'svelte';
import MeasurementAgentsTable from './MeasurementAgentsTable.svelte';
import source from './MeasurementAgentsTable.svelte?raw';
import { measurementScope } from '$lib/stores/measurement';
import type { AgentsState, MeasurementAgentRow } from '$lib/stores/measurementAgents';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	measurementScope.set({ project: 'all', agent: null });
});

const metric = (value: number | null) => ({ value, n: 40, minimum_n: 20, periods: [] });
const rule = (flagged = false) => ({ eligible: true, metric: 'fresh_tokens_per_outcome', minimum_n: 20, flagged });

/** The four columns a served row carries, for rows that only differ in one cell. */
const metricSet = {
	outcomes: metric(762),
	fresh_tokens_per_outcome: metric(123.4),
	first_try_acceptance: metric(0.7795),
	asks_per_outcome: metric(0),
	active_hours_per_outcome: { ...metric(0.05), previous_value: 0.1 },
	dollars_per_work_hour: { ...metric(1.964), previous_value: 2 },
};

const period = {
	grain: 'days' as const, timezone: 'America/Los_Angeles', current: '2026-10-05',
	range: { from: '2026-09-22', to: '2026-10-04' },
	latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' },
};

function agentRow(overrides: Partial<MeasurementAgentRow> = {}): MeasurementAgentRow {
	return {
		agent: { id: 'agent-1', name: 'Quality Gate', project: { id: 'p1', name: 'CORSIA' } },
		n: 320,
		difficulty_mix: { standard: 320 },
		status: 'eligible',
		rank: 1,
		selected_metric: 'fresh_tokens_per_outcome',
		metrics: { ...metricSet },
		worsening: rule(),
		flags: { fresh_tokens_per_outcome: rule(), first_try_acceptance: rule() },
		...overrides,
	};
}

function state(rows: MeasurementAgentRow[], over: Partial<AgentsState> = {}): AgentsState {
	return {
		loading: false, error: null, rows, scopeKey: 'all:none', servedView: 'fresh_tokens_per_outcome:ranked',
		findings: [], findingsState: 'healthy',
		caption: 'Trailing 8 completed weeks', selection: 's', grain: 'days', sort: 'ranked', period,
		projectSummaries: [], inactiveProjects: [], summariesKey: 'all:none:days', ...over,
	};
}

function render(agents: AgentsState): void {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementAgentsTable, { target: host, props: { state: agents } });
	flushSync();
}

describe('MeasurementAgentsTable', () => {
	it('shows the columns of the approved table, in order', () => {
		render(state([agentRow()]));
		const headers = [...host.querySelectorAll('thead th')].map((cell) => (cell.textContent ?? '').trim());
		expect(headers).toEqual([
			'Agent', 'Outcomes', 'First try', '$ per person-hour of work delivered',
			'Change in minutes per outcome, vs Sep 21 to Sep 27',
		]);
		for (const gone of ['Rank', 'Asks', 'Tokens', 'Outcomes / week', '8 weeks before']) expect(host.textContent).not.toContain(gone);
		expect(host.querySelector('h3')?.textContent).toBe('Agents, Sep 28 to Oct 4');
	});

	it('shows each figure in its own unit and the change against the previous weeks', () => {
		render(state([agentRow()]));
		const cells = [...host.querySelectorAll('tbody td.n')].map((cell) => (cell.childNodes[0]?.textContent ?? '').trim());
		expect(cells).toEqual(['320', '78%', '$1.96', '-50%']);
		expect([...host.querySelectorAll('.sr-only')].map((node) => node.textContent).join(' '))
			.not.toContain('0.7795');
	});

	it('labels each cell with the same reading the cell shows', () => {
		render(state([agentRow()]));
		const labelled = [...host.querySelectorAll('tbody td')].filter((cell) => cell.getAttribute('title') !== null);
		expect(labelled.map((cell) => cell.getAttribute('title'))).toEqual(['78%', '$1.96', '-50%']);
	});

	it('says why a change is missing instead of showing zero', () => {
		const thin = agentRow({ n: 400 }); thin.metrics.active_hours_per_outcome = metric(null);
		const fresh = agentRow({ agent: { id: 'agent-2', name: 'Fresh', project: { id: 'p1', name: 'CORSIA' } } });
		fresh.metrics.active_hours_per_outcome = { ...metric(0.05), previous_value: null };
		render(state([thin, fresh]));
		const changes = [...host.querySelectorAll('tbody tr')].map((row) => [...row.querySelectorAll('td')].at(-1)?.textContent?.trim());
		expect(changes).toEqual(['low n', 'new']);
	});

	it('says unmeasured instead of showing an unmeasured first-try share as zero', () => {
		const row = agentRow();
		row.metrics.first_try_acceptance = metric(null);
		render(state([row]));
		const cost = [...host.querySelectorAll('tbody td')].find((cell) => cell.getAttribute('title') === 'Unmeasured');
		expect(cost?.textContent).toContain('Unmeasured');
		expect(cost?.textContent).not.toContain('%');
		expect(cost?.textContent).not.toContain('0');
	});

	it('shows a dash for a dollars figure the server does not serve yet', () => {
		const row = agentRow();
		delete (row.metrics as Record<string, unknown>).dollars_per_work_hour;
		render(state([row]));
		expect([...host.querySelectorAll('tbody td.n')].map((cell) => (cell.childNodes[0]?.textContent ?? '').trim()))
			.toEqual(['320', '78%', '–', '-50%']);
	});

	it('shows the ten agents with the most outcomes, in a single line each', () => {
		// Served ranked by a quality metric, so the row set is not the served first ten.
		render(state(Array.from({ length: 12 }, (_, index) => agentRow({ n: 100 + index, agent: { id: `a${index}`, name: `Agent ${index}`, project: { id: 'p1', name: 'CORSIA' } } }))));
		expect(host.querySelectorAll('tbody tr')).toHaveLength(10);
		expect(host.querySelectorAll('tbody tr:first-child th button.filter')[0]?.getAttribute('aria-label')).toBe('Filter to this agent: Agent 11');
		expect(host.querySelector('tbody')?.textContent).not.toContain('Agent 0');
	});

	it('capitalises the first letter of an agent name wherever the table prints it', () => {
		render(state([agentRow({ agent: { id: 'agent-9', name: 'conductor', project: { id: 'p1', name: 'CORSIA' } } })]));
		expect(host.querySelector('tbody th button.filter')?.textContent).toBe('Conductor');
		expect(host.querySelector('tbody th button.filter')?.getAttribute('aria-label')).toBe('Filter to this agent: Conductor');
	});

	it('leaves out an agent with no outcomes in the window', () => {
		render(state([agentRow({ n: 0, status: 'too_few', rank: null }), agentRow({ n: 5, agent: { id: 'a2', name: 'Busy', project: { id: 'p1', name: 'CORSIA' } } })]));
		expect(host.querySelectorAll('tbody tr')).toHaveLength(1);
	});

	it('leaves a too-few agent in the list and named', () => {
		render(state([agentRow({ rank: null, status: 'too_few', n: 4 })]));
		expect(host.textContent).toContain('Too few outcomes to rank');
		expect(host.querySelector('tbody th')?.textContent).toContain('Quality Gate');
		expect([...host.querySelectorAll('tbody td')][0].textContent).toBe('4');
	});

	it('leaves the needs-a-look marking to Biggest changes', () => {
		render(state([agentRow({ flags: { fresh_tokens_per_outcome: rule(true), first_try_acceptance: rule(false) } })]));
		expect(host.querySelector('.look')).toBeNull();
		expect(host.textContent).not.toContain('Needs a look');
	});

	it('filters when the row is clicked, not only its name', () => {
		render(state([agentRow()]));
		host.querySelector<HTMLElement>('tbody tr td')?.click();
		flushSync();
		expect(get(measurementScope)).toEqual({ project: 'p1', agent: 'agent-1' });
	});

	it('applies the row and its project together through the one scope', () => {
		render(state([agentRow()]));
		host.querySelector<HTMLButtonElement>('button.filter')?.click();
		flushSync();
		expect(get(measurementScope)).toEqual({ project: 'p1', agent: 'agent-1' });
	});

	it('filters to all work when the row belongs to no project', () => {
		const row = agentRow({ agent: { id: 'agent-2', name: 'No Project', project: { id: null, name: null } } });
		render(state([row]));
		host.querySelector<HTMLButtonElement>('button.filter')?.click();
		flushSync();
		expect(get(measurementScope)).toEqual({ project: 'all', agent: 'agent-2' });
	});

	it('renders a failed load as a failure, never as an empty scope', () => {
		// The branch order is the whole difference between an honest "nothing to compare"
		// and a lie: bytes that cannot support the surface leave no rows AND set an error,
		// so the error and its retry must come first and the empty copy must not appear.
		render(state([], { error: 'Could not load the agents table.' }));
		expect(host.textContent).toContain('Could not load the agents table.');
		expect(host.querySelector('[role="alert"]')).not.toBeNull();
		expect(host.textContent).not.toContain('No active agents for this selection.');
		expect(host.querySelector('button')?.textContent).toBe('Retry');
	});

	it('keeps the measured rows on screen when a reload fails for the same view', () => {
		// Only the state that cannot answer is dropped: a retry keeps what it can still claim.
		render(state([agentRow()], { error: 'Could not load the agents table.' }));
		expect(host.querySelector('table')).not.toBeNull();
		expect(host.textContent).not.toContain('No active agents for this selection.');
	});

	it('says there are no agents rather than an empty grid', () => {
		render(state([]));
		expect(host.textContent).toContain('No active agents for this selection.');
		expect(host.textContent).not.toContain('Could not load');
		expect(host.querySelector('table')).toBeNull();
	});

	const named = (id: string, name: string, over: Partial<MeasurementAgentRow> = {}) =>
		agentRow({ agent: { id, name, project: { id: 'p1', name: 'CORSIA' } }, ...over });
	const names = () => [...host.querySelectorAll('tbody th .name')].map((node) => node.textContent);
	const header = (label: string) => [...host.querySelectorAll<HTMLButtonElement>('thead th button.sort')]
		.find((button) => button.textContent === label)!;
	const click = (label: string) => { header(label).click(); flushSync(); };
	const ariaSorts = () => [...host.querySelectorAll('thead th[aria-sort]')].map((cell) => cell.getAttribute('aria-sort'));

	it('has no sort selects: the column headers are the only sort control', () => {
		render(state([agentRow()]));
		expect(host.querySelectorAll('select')).toHaveLength(0);
		expect(host.textContent).not.toContain('Rank by');
		expect(host.textContent).not.toContain('Best first');
		// The table opens in the served order.
		expect(ariaSorts()).toEqual(['none', 'none', 'none', 'none', 'none']);
	});

	it('opens with the most outcomes first, not in the order the server ranked the rows', () => {
		render(state([named('a', 'Few', { n: 5 }), named('b', 'Many', { n: 50 })]));
		expect(names()).toEqual(['Many', 'Few']);
	});

	it('sorts ascending, then descending, then back to the served order', () => {
		const low = named('a', 'Low', { metrics: { ...metricSet, first_try_acceptance: metric(0.1) } });
		const high = named('b', 'High', { metrics: { ...metricSet, first_try_acceptance: metric(0.8) } });
		render(state([high, low]));
		click('First try');
		expect(names()).toEqual(['Low', 'High']);
		expect(header('First try').closest('th')?.getAttribute('aria-sort')).toBe('ascending');
		click('First try');
		expect(names()).toEqual(['High', 'Low']);
		expect(header('First try').closest('th')?.getAttribute('aria-sort')).toBe('descending');
		click('First try');
		expect(names()).toEqual(['High', 'Low']);
		expect(ariaSorts()).toEqual(['none', 'none', 'none', 'none', 'none']);
	});

	it('sorts by agent name and by outcomes', () => {
		render(state([named('a', 'Zed', { n: 5 }), named('b', 'Amy', { n: 50 })]));
		click('Agent');
		expect(names()).toEqual(['Amy', 'Zed']);
		click('Outcomes');
		expect(names()).toEqual(['Zed', 'Amy']);
		click('Outcomes');
		expect(names()).toEqual(['Amy', 'Zed']);
	});

	it('puts an unmeasured value last in either direction and never sorts it as zero', () => {
		const none = named('a', 'None', { metrics: { ...metricSet, first_try_acceptance: metric(null) } });
		const some = named('b', 'Some', { metrics: { ...metricSet, first_try_acceptance: metric(0.4) } });
		render(state([none, some]));
		click('First try');
		expect(names()).toEqual(['Some', 'None']);
		click('First try');
		expect(names()).toEqual(['Some', 'None']);
	});

	it('drops its own card chrome when embedded in another card', () => {
		render(state([agentRow()]));
		expect(host.querySelector('.agents.embedded')).toBeNull();
		unmount(component!); host.remove();
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(MeasurementAgentsTable, { target: host, props: { state: state([agentRow()]), embedded: true } });
		flushSync();
		expect(host.querySelector('.agents.embedded')).not.toBeNull();
	});

	it('groups the outcome count the way every other count on the page is grouped', () => {
		render(state([agentRow({ n: 12400 })]));
		expect(host.querySelector('tbody td.n')?.textContent).toBe('12,400');
	});

	it('puts the table in one horizontal-scroll region a keyboard reader can reach', () => {
		render(state([agentRow()]));
		const wrap = host.querySelector('.table-wrap');
		expect(wrap?.getAttribute('role')).toBe('region');
		expect(wrap?.getAttribute('tabindex')).toBe('0');
		expect(wrap?.getAttribute('aria-label')).toContain('Agents table');
	});

	it('keeps every column in view at 430px and under, with room for the dated header', () => {
		render(state([agentRow()]));
		const headers = [...host.querySelectorAll('thead th')];
		expect(headers[3].classList.contains('cost') && headers[3].textContent).toBe('$ per person-hour of work delivered');
		expect(headers[3].querySelector('button')?.getAttribute('aria-label'), 'a phone reader still hears the full name').toBe('$ per person-hour of work delivered');
		expect(headers[3].querySelector('.long')?.textContent, 'only the tail of the name drops on a phone').toBe('of work delivered');
		expect(headers[4].classList.contains('change') && headers[4].textContent).toBe('Change in minutes per outcome, vs Sep 21 to Sep 27');
		const css = source.slice(source.indexOf('<style>'));
		const at = css.indexOf('@media (max-width: 430px)');
		expect(at, 'a phone block exists').toBeGreaterThan(-1);
		const phone = css.slice(at, css.indexOf('\n\t}', at));
		// No floor on the table or the headers, so the columns share the width instead of scrolling past the edge.
		expect(phone).toContain('table { width: 100%; min-width: 0; table-layout: auto; font-size: var(--text-footnote); }');
		expect(phone).not.toContain('min-width: 11ch');
		expect(phone).not.toContain('min-width: 20ch');
		// Long names can break to fit; headers wrap between words.
		expect(phone).toContain('tbody th { white-space: normal; }');
		expect(phone).toMatch(/\.filter \{[^}]*max-width: 100%;[^}]*overflow-wrap: anywhere;[^}]*word-break: normal;/);
		expect(phone).toMatch(/thead th \{[^}]*white-space: normal;[^}]*word-break: normal;/);
		// Numeric cells take their content width, leaving the remaining space for agent names.
		expect(phone).toContain('thead th.n { width: 1%; }');
		expect(phone).toContain('tbody td.n { white-space: nowrap; }');
	});
});
