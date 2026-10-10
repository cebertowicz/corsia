import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import MeasurementPage from './MeasurementPage.svelte';
import { measurementAgents, measurementAgentsView } from '$lib/stores/measurementAgents';
import { projects as projectStore } from '$lib/stores/projects';
import { token } from '$lib/stores/auth';
import { servedBody, servedRow, servedSummary } from '../__tests__/fixtures/measurementAgents';
import type { AgentsRequestContract } from '$lib/api/measurementAgentsTypes';
import { loadMeasurementPanel, measurementPanels, measurementScope } from '$lib/stores/measurement';
import { measurementGrain } from '$lib/stores/measurementGrain';
import { periodsPeriod } from '../__tests__/fixtures/measurementPeriod';

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.unstubAllGlobals();
	measurementPanels.set({});
	measurementAgents.set({ loading: true, error: null, rows: [], findings: [], findingsState: null, caption: '', selection: '', scopeKey: '', servedView: null, grain: null, sort: null, projectSummaries: [], inactiveProjects: [], summariesKey: null });
	projectStore.set([]);
	measurementGrain.set('days');
	measurementScope.set({ project: 'all', agent: null });
	token.set(null);
	window.history.replaceState({}, '', '/');
});

const row = (series: string, value: number | null) => ({ period: '2026-09-21', series, value, n: 762, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false });

describe('MeasurementPage headlines', () => {
	it('rounds a small ratio headline without discarding digits a larger value carries', async () => {
		const bySeries: Record<string, number> = { outcomes: 762, active_hours_per_outcome: 0.03, asks_per_outcome: 0.131233, first_try_acceptance: 0.7795 };
		vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => {
			if (url.includes('/series/index')) return { definitions: [] };
			if (!url.includes('/api/measurement/series?')) return {};
			const ids = new URL(url, 'http://x').searchParams.get('ids')?.split(',') ?? [];
			return { period: periodsPeriod('2026-09-21'), windows: Object.fromEntries(ids.map((id) => [id, { latest: { value: bySeries[id] ?? null, n: 762 }, previous: { value: null, n: 0 } }])), rows: ids.flatMap((id) => [row(id, bySeries[id] ?? null), { ...row(id, 99), period: '2026-09-28', comparable: false }, { ...row(id, 100), period: '2026-10-05', partial: true }]) };
		} })));
		host = document.createElement('div');
		document.body.appendChild(host);
		// PL-4: All work is the project list, so a headline needs a drilled scope.
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=days');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.kept-panels svg .bar').length).toBeGreaterThan(0));
		flushSync();

		// r2.7 has no headline strip: its measures live on as the charts below.
		expect(host.querySelector('.headlines')).toBeNull();
			// Minutes per outcome and First try are week bar charts; Asks is one line under the minutes' agents table and a kept chart of its own.
			const minutes = host.querySelector('#attention-title')?.closest('section');
			expect(host.querySelector('#attention-title')?.textContent).toBe('Your minutes per outcome');
			expect(minutes?.querySelector('.stat')?.textContent).toBe('1.8 minutes per outcome, Aug 31 to Sep 27, n 762, new');
			expect(minutes?.querySelector('.asks')?.textContent).toBe('Asks per outcome, all agents: 0.13, new');
			expect(minutes?.querySelector('svg')).not.toBeNull();
			expect(host.querySelector('#first-try-title')?.closest('section')?.querySelector('.stat')?.textContent).toBe('78% first try, Aug 31 to Sep 27, n 762, new');
			expect(host.querySelector('#asks-title')?.closest('.kept-panels')).not.toBeNull();
			expect(host.querySelector('#asks-title')?.closest('section')?.querySelector('.stat')?.textContent).toBe('0.13 asks per outcome, Aug 31 to Sep 27, n 762, new');
			expect(host.querySelector('.source-line')).toBeNull();
			expect(host.querySelector('.attention-summary')).toBeNull();
		expect(host.querySelector('#outcomes-title')?.closest('section')?.querySelector('svg .bar')).not.toBeNull();
		expect(host.querySelector('#tokens-title')?.textContent).toBe('Fresh tokens per outcome');
		const sizing = host.querySelector('.sizing-headline');
		expect(host.querySelector('.sizing-notice')).toBeNull();
		// The served rows here carry no size coverage, so no number is invented for them.
		expect(sizing?.querySelector('.stat')).toBeNull();
		expect(sizing?.textContent).toContain('No completed weeks in this range.');
		// Nothing explains a hidden current period: r2.7 has no such line and the partial period is shown.
		expect(host.querySelector('.short-history')).toBeNull();
	});

	it('explains a week-less history once, never with two lines', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => {
			if (url.includes('/series/index')) return { definitions: [] };
			if (url.includes('/api/measurement/series?')) return { rows: [] };
			return {};
		} })));
		host = document.createElement('div');
		document.body.appendChild(host);
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=days');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(host.querySelector('.headlines-pending')?.textContent).toBe('Daily numbers start with your first recorded day.'));
		expect(host.querySelector('.short-history')).toBeNull();
		expect(host.textContent).not.toContain('Charts show completed weeks');
		expect(host.querySelector('.kept-panels')).toBeNull();
		expect(host.textContent).not.toContain('No measurement rows');
	});

	it('keeps the weekly panels when only a panel other than outcomes has completed weeks', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => {
			if (url.includes('/series/index')) return { definitions: [] };
			if (!url.includes('/api/measurement/series?')) return {};
			const ids = new URL(url, 'http://x').searchParams.get('ids')?.split(',') ?? [];
			return { rows: ids.includes('took_recommendation_pct') ? [row('took_recommendation_pct', 0.8)] : [] };
		} })));
		host = document.createElement('div');
		document.body.appendChild(host);
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=days');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(['outcomes', 'size', 'alignment'].every((id) => get(measurementPanels)[id]?.loading === false)).toBe(true));
		flushSync();
		expect(host.querySelector('.kept-panels')).not.toBeNull();
		expect(host.querySelector('.headlines-pending')).toBeNull();
		expect(host.querySelector('#alignment-title')).not.toBeNull();
	});

	it('answers over the four weeks ending at the latest completed week even when that week has no rate', async () => {
		const size = (period: string, value: number | null, sized: number) => ({
			...row('estimated_human_hours_per_active_hour', value), period, n: sized, proxy: true, partial: sized < 30,
			size: { outcomes: 30, sized, estimated_hours: sized * 2, active_hours: 0.2, unsized: 30 - sized - 2, unknown: 1, stale: 1, conflict: 0, retracted: 0, malformed: 0,
				basis: { delivered_evidence_proxy: sized, accepted_scope_proxy: 0, author_declared: 0 },
				profiles: [{ method: 'offline_proxy', method_version: '1', rubric_version: '1.3p', rater: 'synthetic-rater', rater_version: 'unknown', outcomes: sized }],
				comparable_reason: null, rate_reason: value === null ? 'below_minimum' : null, class_midpoint_hours: { XS: 0.1, S: 0.5, M: 2, L: 8, XL: 40, XXL: 120 },
				data_start: '2026-08-31', comparability_before: '2026-08-31' },
		});
		vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => {
			if (!url.includes('/api/measurement/series?')) return {};
			if (!url.includes('estimated_human_hours_per_active_hour')) return { rows: [] };
			// The served window pools Sep 7 to Oct 4: 54 estimated hours over 0.4 active hours, 27 of 60 outcomes sized; nothing before.
			return { period: periodsPeriod('2026-09-28'), rows: [size('2026-09-21', 200, 24), size('2026-09-28', null, 3)], windows: { estimated_human_hours_per_active_hour: {
				latest: { value: 135, n: 27, size: { outcomes: 60, sized: 27, excluded: 33, estimated_hours: 54, active_hours: 0.4 } },
				previous: { value: null, n: 0, size: { outcomes: 0, sized: 0, excluded: 0, estimated_hours: 0, active_hours: 0 } } } } };
		} })));
		host = document.createElement('div');
		document.body.appendChild(host);
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=days');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(host.querySelector('.sizing-headline .stat')).not.toBeNull());
		const sizing = host.querySelector('.sizing-headline') as HTMLElement;
		// The answer covers the four-week window ending at the latest week; the unrated latest week adds its sized subtotal, not a zero.
		expect(sizing.querySelector('.stat')?.textContent).toBe('135 h of work per hour you spend, Sep 7 to Oct 4, n 27, new');
		expect(sizing.querySelector('.coverage')).toBeNull();
		expect(sizing.querySelector('.nf')?.textContent).toContain('33 of 60 accepted outcomes in Sep 7 to Oct 4 have no usable size rating');
		// The server served these weeks as comparable, so the headline says nothing about comparability.
		expect(sizing.querySelector('.comparability')).toBeNull();
		expect(sizing.querySelector('.definition')).toBeNull();
		expect(host.querySelector('.tm')).toBeNull();

		// A failed refresh keeps the weeks on screen and offers Retry beside them.
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })));
		await loadMeasurementPanel('size');
		flushSync();
		expect(sizing.querySelector('.stat')?.textContent).toContain('135 h of work per hour you spend');
		expect(sizing.querySelector('[role="alert"] button')?.textContent).toBe('Retry');
	});

	it('keeps the URL the app owns and records its own selection beside it', async () => {
		window.history.replaceState(null, '', '/?thread=abc');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ rows: [], definitions: [] }) })));
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(host.querySelector('.controls')).not.toBeNull());
		measurementGrain.set('weeks');
		flushSync();
		// PL-2: the selection is durable in the URL, but it is added to the query the
		// app already carries and never replaces it -- a thread to open survives a grain change.
		const params = new URLSearchParams(window.location.search);
		expect(params.get('thread')).toBe('abc');
		expect(params.get('grain')).toBe('weeks');
		expect(params.get('project')).toBe('all');
	});

	it('asks every measurement route for the grain and the viewer\'s zone, never for a range or lessons', async () => {
		const urls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (url: string) => { urls.push(String(url)); return { ok: true, json: async () => ({ rows: [], definitions: [] }) }; }));
		host = document.createElement('div');
		document.body.appendChild(host);
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=weeks');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(urls.some((url) => url.includes('/api/measurement/where-to-look'))).toBe(true));
		const measurement = urls.filter((url) => url.includes('/api/measurement/'));
		expect(measurement.length).toBeGreaterThan(4);
		for (const url of measurement) {
			const query = new URL(url, 'http:' + '//x').searchParams;
			expect(query.get('grain'), url).toBe('weeks');
			expect(query.get('timezone'), url).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
			expect(query.has('range'), url).toBe(false);
		}
		expect(urls.some((url) => url.includes('/lessons'))).toBe(false);
		// Biggest changes names the page's own series request, so the server answers both from one read.
		const ids = (route: string) => new URL(measurement.find((url) => url.includes(route))!, 'http:' + '//x').searchParams.get('ids');
		expect(ids('/api/measurement/where-to-look?')).toBe(ids('/api/measurement/series?'));
	});

	it('offers a scope picker and a Days | Weeks control that starts on Days, and no agent chip without an agent', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ rows: [], definitions: [] }) })));
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(host.querySelector('.controls')).not.toBeNull());
		flushSync();
		expect(host.querySelector('.controls')?.textContent).not.toContain('Trend range');
		expect(host.querySelector('.tm')).toBeNull();
		const trigger = host.querySelector('.picker-trigger') as HTMLButtonElement;
		expect(trigger.getAttribute('aria-label')).toBe('Measurement scope: All work');
		const buttons = [...host.querySelectorAll('.controls [role="group"][aria-label="Grain"] button')] as HTMLButtonElement[];
		expect(buttons.map((button) => [button.textContent, button.getAttribute('aria-pressed')])).toEqual([['Days', 'true'], ['Weeks', 'false']]);
		buttons[1].click();
		flushSync();
		expect(get(measurementGrain)).toBe('weeks');
		expect(new URLSearchParams(window.location.search).get('grain')).toBe('weeks');
		expect(host.querySelector('.agent-chip')).toBeNull();
	});
});

describe('MeasurementPage values belong to the selection that loaded them', () => {
	const outcomesPanel = () => host.querySelector('#outcomes-title')?.closest('section');
	const outcomesDrawn = () => outcomesPanel()?.querySelector('svg .bar') ?? null;

	it('withholds the previous scope under a scope that failed, and keeps a same-scope retry', async () => {
		let failing = false;
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (!url.includes('/api/measurement/series?')) return { ok: true, json: async () => ({}) };
			if (failing) return { ok: false, json: async () => ({}) };
			const ids = new URL(url, 'http://x').searchParams.get('ids')?.split(',') ?? [];
			return { ok: true, json: async () => ({ rows: ids.map((id) => row(id, id === 'outcomes' ? 762 : null)) }) };
		}));
		host = document.createElement('div');
		document.body.appendChild(host);
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=days');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(outcomesDrawn()).not.toBeNull());

		failing = true;
		measurementScope.set({ project: 'beta', agent: null });
		await vi.waitFor(() => expect(outcomesPanel()?.querySelector('[role="alert"]')).not.toBeNull());
		flushSync();
		expect(outcomesPanel()?.querySelector('svg .bar')).toBeNull();
		expect(outcomesPanel()?.querySelector('[role="alert"] button')?.textContent).toBe('Retry');

		failing = false;
		measurementScope.set({ project: 'prj-atlas', agent: null });
		await vi.waitFor(() => expect(outcomesDrawn()).not.toBeNull());
		failing = true;
		await loadMeasurementPanel('outcomes');
		flushSync();
		expect(outcomesPanel()?.querySelector('svg .bar')).not.toBeNull();
		expect(outcomesPanel()?.querySelector('[role="alert"] button')?.textContent).toBe('Retry');
	});
});

describe('MeasurementPage agents reloads have one owner', () => {
	afterEach(() => {
		measurementAgentsView.set({ metric: 'fresh_tokens_per_outcome', sort: 'ranked' });
	});

	it('sends one agents request when the reader changes the column or the order', async () => {
		const agentsRequests: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (String(url).includes('/api/measurement/agents')) agentsRequests.push(String(url));
			return {
				ok: true,
				json: async () => ({ rows: [], definitions: [], findings: [], findings_state: 'healthy', caption: 'Trailing 8 completed weeks', period: { grain: 'days' }, sort: 'ranked' }),
			};
		}));
		host = document.createElement('div');
		document.body.appendChild(host);
		// The Agents table owns the column and order controls, and PL-4 keeps it out of
		// the All-work view, so this proof runs at a drilled scope.
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=days');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		component = mount(MeasurementPage, { target: host });
		await vi.waitFor(() => expect(agentsRequests.length).toBeGreaterThan(0));
		const before = agentsRequests.length;

		// A new column is a new order claim, so the page reloads -- once. The page's own
		// effect owns that reload, so the control must not fire a second ask that the
		// first then aborts.
		measurementAgentsView.set({ metric: 'outcomes', sort: 'ranked' });
		flushSync();
		await vi.waitFor(() => expect(agentsRequests.length).toBe(before + 1));
		await new Promise((settle) => setTimeout(settle, 50));
		expect(agentsRequests.length).toBe(before + 1);
	});
});

const PROJECTS = [
	{ id: 'prj-boreas', name: 'Boreas', status: 'active', accent_color: null },
	{ id: 'prj-atlas', name: 'Atlas', status: 'active', accent_color: null },
];
const BUGS = { id: 'ag-bugs', name: 'Bugs', project: { id: 'prj-boreas', name: 'Boreas' } };

/** The request one recorded URL actually asked for, read back rather than assumed. */
function requestFromUrl(text: string): AgentsRequestContract {
	const params = new URL(text, 'http://x').searchParams;
	const agent = params.get('agent');
	return {
		project: params.get('project') ?? 'all',
		agent: agent && agent !== 'none' ? agent : null,
		metric: (params.get('metric') ?? 'fresh_tokens_per_outcome') as AgentsRequestContract['metric'],
		sort: (params.get('sort') ?? 'ranked') as AgentsRequestContract['sort'],
		grain: (params.get('grain') ?? 'days') as AgentsRequestContract['grain'],
	};
}

/**
 * Stub the routes this page asks, answering each Agents call with an echo bound to the
 * URL that asked it. A body bound to the live stores instead would answer a selection
 * the reader already left and get discarded, which is the right outcome for the store
 * but never a way to prove what the surface renders.
 */
function stubRoutes(agents: (request: AgentsRequestContract) => Record<string, unknown>): string[] {
	const agentsRequests: string[] = [];
	vi.stubGlobal('fetch', vi.fn(async (url: string) => {
		const text = String(url);
		if (text.includes('/api/measurement/agents')) {
			agentsRequests.push(text);
			return { ok: true, json: async () => agents(requestFromUrl(text)) };
		}
		if (text.includes('/api/projects')) return { ok: true, json: async () => ({ projects: PROJECTS }) };
		return { ok: true, json: async () => ({ rows: [], definitions: [] }) };
	}));
	return agentsRequests;
}

function renderPage(): HTMLElement {
	token.set('test-token');
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MeasurementPage, { target: host });
	flushSync();
	return host;
}

async function openScopePicker(): Promise<void> {
	(host.querySelector('.picker-trigger') as HTMLButtonElement).click();
	await vi.waitFor(() => expect(host.querySelector('.picker-search')).not.toBeNull());
	flushSync();
}

async function typeSearch(term: string): Promise<void> {
	const input = host.querySelector('.picker-search') as HTMLInputElement;
	input.value = term;
	input.dispatchEvent(new Event('input', { bubbles: true }));
	await vi.waitFor(() => expect(host.querySelectorAll('.picker-option').length).toBeGreaterThan(0));
	flushSync();
}

describe('MeasurementPage scope selection', () => {
	it('shows the parent project on the trigger and the agent on the chip, and asks for both together', async () => {
		const requests = stubRoutes((request) => servedBody(request, {
			rows: [servedRow(request, { agent: BUGS })], project_summaries: [], inactive_projects: [],
		}));
		renderPage();
		await vi.waitFor(() => expect(host.querySelector('.picker-trigger')?.getAttribute('aria-label')).toBe('Measurement scope: All work'));

		await openScopePicker();
		await typeSearch('bugs');
		(host.querySelector('.picker-option.is-nested') as HTMLButtonElement).click();
		flushSync();

		await vi.waitFor(() => expect(get(measurementScope)).toEqual({ project: 'prj-boreas', agent: 'ag-bugs' }));
		flushSync();
		// The trigger names where the reader is looking; the chip names what removing it changes.
		expect(host.querySelector('.picker-trigger')?.getAttribute('aria-label')).toBe('Measurement scope: Boreas');
		// The scope's name is the app header's; the page does not title itself a second time.
		expect(host.querySelector('.header h2')).toBeNull();
		expect(host.querySelector('.header .scope-label')?.textContent).toBe('Scope');
		expect(host.querySelector('.header p')).toBeNull();
		expect(host.querySelector('.agent-chip')?.textContent).toContain('Bugs');
		const asked = new URL(requests.at(-1)!, 'http://x').searchParams;
		expect(asked.get('project')).toBe('prj-boreas');
		expect(asked.get('agent')).toBe('ag-bugs');
	});

	it('keeps the agent when the reader widens the project to All work, and the chip removes it', async () => {
		stubRoutes((request) => servedBody(request, {
			rows: [servedRow(request, { agent: BUGS })], project_summaries: [], inactive_projects: [],
		}));
		renderPage();
		await vi.waitFor(() => expect(host.querySelector('.picker-trigger')?.getAttribute('aria-label')).toBe('Measurement scope: All work'));

		await openScopePicker();
		await typeSearch('bugs');
		(host.querySelector('.picker-option.is-nested') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.querySelector('.agent-chip')).not.toBeNull());

		await openScopePicker();
		await typeSearch('all work');
		(host.querySelector('.picker-option') as HTMLButtonElement).click();
		// Widening the project keeps the agent: every agent is inside All work, so the
		// composite writer drops only an agent that cannot belong to the project named.
		await vi.waitFor(() => expect(get(measurementScope)).toEqual({ project: 'all', agent: 'ag-bugs' }));
		expect(host.querySelector('.agent-chip')).not.toBeNull();

		// The chip is the removable half of the contract, not the trigger.
		(host.querySelector('.agent-chip button') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(get(measurementScope)).toEqual({ project: 'all', agent: null }));
		expect(host.querySelector('.agent-chip')).toBeNull();
	});
});

describe('MeasurementPage at All work shows every chart, never a project list', () => {
	it('asks for the series, Models and Biggest changes at All work and renders no project list', async () => {
		const urls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			const text = String(url);
			urls.push(text);
			if (text.includes('/api/measurement/agents')) return { ok: true, json: async () => servedBody(requestFromUrl(text), { rows: [], findings: [], findings_state: 'no_agents', project_summaries: [], inactive_projects: [] }) };
			if (text.includes('/api/projects')) return { ok: true, json: async () => ({ projects: PROJECTS }) };
			return { ok: true, json: async () => ({ rows: [], definitions: [] }) };
		}));
		renderPage();
		await vi.waitFor(() => expect(urls.some((url) => url.includes('/api/measurement/series?'))).toBe(true));
		await vi.waitFor(() => expect(urls.some((url) => url.includes('/api/measurement/models?') && url.includes('project=all'))).toBe(true));
		await vi.waitFor(() => expect(urls.some((url) => url.includes('/api/measurement/where-to-look?') && url.includes('project=all'))).toBe(true));
		expect(host.querySelector('.project-list')).toBeNull();
		expect(host.querySelector('section[aria-label="Cost"]')).not.toBeNull();
		expect(host.querySelector('[aria-label="Models comparison"]')).not.toBeNull();
		expect(host.querySelector('#measurement-where-to-look')).not.toBeNull();
	});
});

describe('MeasurementPage reading order on a drilled scope', () => {
	it('reads work per hour, Cost with Models inside it, minutes, first try, the other panels, then Biggest changes and Agents', async () => {
		stubRoutes((request) => servedBody(request, {
			rows: [], findings: [], findings_state: 'no_agents', project_summaries: [], inactive_projects: [],
		}));
		window.history.replaceState({}, '', '/?project=prj-atlas&agent=none&grain=days');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		renderPage();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Models comparison"]')).not.toBeNull());
		const selectors = ['.sizing-headline', '[aria-label="Cost"]', '[aria-label="Models comparison"]', '#attention-title', '#first-try-title', '.kept-panels', '.agents'];
		const found = selectors.map((selector) => host.querySelector(selector));
		expect(found.map((element) => element !== null)).toEqual(selectors.map(() => true));
		for (let index = 1; index < found.length; index += 1) {
			expect(found[index - 1]!.compareDocumentPosition(found[index]!) & Node.DOCUMENT_POSITION_FOLLOWING, `${selectors[index]} follows ${selectors[index - 1]}`).toBeTruthy();
		}
		// The Agents table lives inside the Biggest changes card, not beside a separate findings panel.
		expect(host.querySelector('#measurement-where-to-look .agents.embedded')).not.toBeNull();
		expect(host.querySelector('.needs-look')).toBeNull();
		// Models is part of the Cost section, not a section of its own at the bottom.
		expect(host.querySelector('[aria-label="Cost"] [aria-label="Models comparison"], [aria-label="Cost"] .models')).not.toBeNull();
	});
});

describe('MeasurementPage Close (PL-2)', () => {
	it('offers Close on every entry, including one with no doorway behind it', async () => {
		stubRoutes((request) => servedBody(request, {
			rows: [], findings: [], findings_state: 'no_agents', project_summaries: [], inactive_projects: [],
		}));
		let closed = 0;
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('test-token');
		// HomeShell always supplies the callback, so a rail visit gets a Close too:
		// where it goes is the origin store's promise (measurementNavigation.test.ts),
		// and this proves the surface never traps the reader in it.
		component = mount(MeasurementPage, { target: host, props: { onClose: () => { closed += 1; } } });
		flushSync();
		await vi.waitFor(() => expect(get(measurementScope)).toEqual({ project: 'all', agent: null }));
		expect(host.querySelector('.close-page')).not.toBeNull();
		(host.querySelector('.close-page') as HTMLButtonElement).click();
		expect(closed).toBe(1);
	});
});
