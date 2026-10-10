import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { measurementScope } from './measurement';
import { measurementGrain } from './measurementGrain';
import {
	loadMeasurementAgents,
	measurementAgents,
	measurementAgentsView,
} from './measurementAgents';
import type { AgentsRequestContract } from '../api/measurementAgentsTypes';
import { servedBody, servedFinding, servedRow } from '../__tests__/fixtures/measurementAgents';
import { token } from './auth';

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	measurementAgents.set({ loading: true, error: null, rows: [], findings: [], findingsState: null, caption: '', selection: '', scopeKey: '', servedView: null, grain: null, sort: null, projectSummaries: [], inactiveProjects: [], summariesKey: null });
	measurementAgentsView.set({ metric: 'fresh_tokens_per_outcome', sort: 'ranked' });
	measurementGrain.set('days');
	measurementScope.set({ project: 'all', agent: null });
	token.set(null);
});

describe('Agents requests', () => {
	/**
	 * The selection the store is about to ask for, read when the answer is produced, so
	 * every echo in these bodies names the request that was actually made. The Agents
	 * contract rejects bytes that describe a different scope, grain, column or order: a
	 * fixture cannot answer one request while echoing another.
	 */
	const currentRequest = (): AgentsRequestContract => ({
		project: get(measurementScope).project,
		agent: get(measurementScope).agent,
		metric: get(measurementAgentsView).metric,
		sort: get(measurementAgentsView).sort,
		grain: get(measurementGrain),
	});
	const empty = (overrides: Record<string, unknown> = {}) => servedBody(currentRequest(), { rows: [], findings: [], project_summaries: [], inactive_projects: [], ...overrides });
	/** One measured agent, served under whatever identity the test asks for. */
	const row = (agent: { id: string; name: string; project: { id: string | null; name: string | null } }) => servedRow(currentRequest(), { agent });
	const idle = { id: 'a', name: 'Idle', project: { id: null, name: null } };

	it('asks the agents route for the composite scope, the grain, the column and the order', async () => {
		token.set('test-token');
		measurementScope.set({ project: 'project-1', agent: 'agent-1' });
		measurementAgentsView.set({ metric: 'first_try_acceptance', sort: 'worsening' });
		const fetchMock = vi.fn(async (_url: string) => ({ ok: true, json: async () => empty() }));
		vi.stubGlobal('fetch', fetchMock);
		await loadMeasurementAgents();
		const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://x');
		expect(url.pathname).toBe('/api/measurement/agents');
		expect([...url.searchParams.keys()].sort()).toEqual(['agent', 'grain', 'metric', 'project', 'sort', 'timezone', 'token']);
		expect(url.searchParams.get('project')).toBe('project-1');
		expect(url.searchParams.get('agent')).toBe('agent-1');
		expect(url.searchParams.get('grain')).toBe('days');
		expect(url.searchParams.get('metric')).toBe('first_try_acceptance');
		expect(url.searchParams.get('sort')).toBe('worsening');
		expect(get(measurementAgents).findingsState).toBe('healthy');
		expect(get(measurementAgents).caption).toBe('Trailing 8 completed weeks');
	});

	it('binds the order the rows were served under, so a column cannot claim an order it is not in', async () => {
		vi.stubGlobal('fetch', vi.fn(async (_url: string) => ({ ok: true, json: async () => empty() })));
		measurementAgentsView.set({ metric: 'fresh_tokens_per_outcome', sort: 'ranked' });
		await loadMeasurementAgents();
		expect(get(measurementAgents).sort).toBe('ranked');

		// A different order is a different order claim: the rows and the order they were
		// served in leave together, so nothing is left on screen claiming an order it does
		// not actually show while the new one is in flight. The automatic findings do NOT
		// leave -- they answer for the scope and cannot move with the column.
		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async () => {
			await gate;
			return { ok: true, json: async () => empty() };
		}));
		measurementAgentsView.set({ metric: 'fresh_tokens_per_outcome', sort: 'worsening' });
		const pending = loadMeasurementAgents();
		expect(get(measurementAgents).sort).toBeNull();
		expect(get(measurementAgents).rows).toEqual([]);
		expect(get(measurementAgents).findingsState).toBe('healthy');
		expect(get(measurementAgents).caption).toBe('Trailing 8 completed weeks');
		release();
		await pending;
		expect(get(measurementAgents).sort).toBe('worsening');
	});

	it('keeps the scope-bound automatic findings across grain, column and order reloads', async () => {
		const served = () => empty({ findings: [servedFinding({ agent: idle, standardized_size: 5, relative_adverse_change: 1.25, action: 'Look at this agent first' })] });
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => served() })));
		await loadMeasurementAgents();
		expect(get(measurementAgents).findings.length).toBe(1);
		expect(get(measurementAgents).findingsState).toBe('healthy');

		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async () => { await gate; return { ok: true, json: async () => served() }; }));

		// A Trend grain change, then a column change, then an order change. None of them
		// can change what the automatic findings found, so the panel keeps answering
		// through the reload instead of flashing to nothing over data that did not move.
		measurementGrain.set('weeks');
		const first = loadMeasurementAgents();
		await Promise.resolve();
		expect(get(measurementAgents).findings.length).toBe(1);
		expect(get(measurementAgents).findingsState).toBe('healthy');
		release();
		await first;

		release = () => {};
		const gate2 = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async () => { await gate2; return { ok: true, json: async () => served() }; }));
		measurementAgentsView.set({ metric: 'outcomes', sort: 'ranked' });
		const second = loadMeasurementAgents();
		await Promise.resolve();
		expect(get(measurementAgents).findings.length).toBe(1);
		expect(get(measurementAgents).findingsState).toBe('healthy');
		release();
		await second;
	});

	it('clears everything when the scope changes, because findings answer to the scope', async () => {
		const served = () => empty({ rows: [row(idle)] });
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => served() })));
		await loadMeasurementAgents();
		expect(get(measurementAgents).findingsState).toBe('healthy');
		expect(get(measurementAgents).rows.length).toBe(1);

		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async () => { await gate; return { ok: true, json: async () => empty() }; }));
		measurementScope.set({ project: 'project-1', agent: null });
		const pending = loadMeasurementAgents();
		expect(get(measurementAgents).findingsState).toBeNull();
		expect(get(measurementAgents).rows).toEqual([]);
		expect(get(measurementAgents).caption).toBe('');
		release();
		await pending;
	});

	it('reloads for a new grain without blanking rows that cannot move with the grain', async () => {
		let request = 0;
		const fetchMock = vi.fn(async (url: string) => ({
			ok: true,
			json: async () => empty({ rows: [row({ id: `agent-${request}`, name: 'x', project: { id: null, name: null } })] }),
		}));
		vi.stubGlobal('fetch', fetchMock);
		await loadMeasurementAgents();
		expect(get(measurementAgents).grain).toBe('days');
		request = 1;
		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			await gate;
			return { ok: true, json: async () => empty({ rows: [row({ id: 'agent-1', name: 'x', project: { id: null, name: null } })] }) };
		}));
		// The reader changes the Trend grain: only the project summaries answer for it.
		// The request still goes out and the served grain updates, but the rows and the
		// scope-bound findings stay on screen rather than flashing to skeletons.
		measurementGrain.set('weeks');
		const pending = loadMeasurementAgents();
		expect(get(measurementAgents).rows.length).toBe(1);
		expect(get(measurementAgents).findingsState).toBe('healthy');
		release();
		await pending;
		expect(get(measurementAgents).grain).toBe('weeks');
		expect(get(measurementAgents).rows[0].agent.id).toBe('agent-1');
	});

	it('discards a response for a selection the reader already left', async () => {
		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		const fetchMock = vi.fn(async (url: string) => {
			if (String(url).includes('metric=outcomes')) await gate;
			return { ok: true, json: async () => empty({ rows: [row({ id: String(url), name: 'x', project: { id: null, name: null } })] }) };
		});
		vi.stubGlobal('fetch', fetchMock);
		// The first ask is for a column the reader then leaves: its late answer
		// must not land under the column now shown.
		measurementAgentsView.set({ metric: 'outcomes', sort: 'ranked' });
		const slow = loadMeasurementAgents();
		await Promise.resolve();
		measurementAgentsView.set({ metric: 'fresh_tokens_per_outcome', sort: 'ranked' });
		await loadMeasurementAgents();
		release();
		await slow;
		expect(get(measurementAgents).rows[0].agent.id).toContain('metric=fresh_tokens_per_outcome');
	});

	it('reports an error without leaving another selection\'s rows behind', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })));
		await loadMeasurementAgents('days');
		expect(get(measurementAgents).error).toBe('Could not load the agents table.');
		expect(get(measurementAgents).rows).toEqual([]);
		expect(get(measurementAgents).sort).toBeNull();
		expect(get(measurementAgents).findingsState).toBeNull();
	});

	it('keeps the served grain\'s project summaries with the scope and grain they answer for', async () => {
		const served = () => empty({
			project_summaries: [{ project: { id: 'p1', name: 'Alpha' }, flagged_agents: 1, outcomes: 160 }],
			inactive_projects: [{ id: 'p2', name: 'Beta' }],
		});
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => served() })));
		await loadMeasurementAgents();
		const state = get(measurementAgents);
		expect(state.projectSummaries).toEqual(served().project_summaries);
		expect(state.inactiveProjects).toEqual(served().inactive_projects);
		// The key names both halves of what the lists answer for, so a consumer can
		// never present them under a scope or a grain they were not counted for.
		expect(state.summariesKey).toBe('all:none:days');
	});

	it('re-answers the project summaries for a new grain instead of showing the old ones as current', async () => {
		const narrow = () => empty({
			rows: [row({ id: 'a', name: 'x', project: { id: 'p1', name: 'Alpha' } })],
			findings_state: 'flagged',
			findings: [servedFinding({ agent: { id: 'a', name: 'x', project: { id: 'p1', name: 'Alpha' } } })],
			project_summaries: [{ project: { id: 'p1', name: 'Alpha' }, flagged_agents: 1, outcomes: 270 }],
			inactive_projects: [{ id: 'p2', name: 'Beta' }],
		});
		const wide = () => empty({
			rows: [row({ id: 'a', name: 'x', project: { id: 'p1', name: 'Alpha' } })],
			findings_state: 'flagged',
			findings: [servedFinding({ agent: { id: 'a', name: 'x', project: { id: 'p1', name: 'Alpha' } } })],
			project_summaries: [{ project: { id: 'p1', name: 'Alpha' }, flagged_agents: 1, outcomes: 160 }],
			inactive_projects: [],
		});
		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => narrow() })));
		await loadMeasurementAgents();
		expect(get(measurementAgents).projectSummaries[0].outcomes).toBe(270);

		vi.stubGlobal('fetch', vi.fn(async () => { await gate; return { ok: true, json: async () => wide() }; }));
		measurementGrain.set('weeks');
		const pending = loadMeasurementAgents();
		// The wide counts have not arrived, so nothing may claim the 270 outcome week
		// describes 26 weeks. The fixed-frame rows and the scope-bound findings stay.
		expect(get(measurementAgents).projectSummaries).toEqual([]);
		expect(get(measurementAgents).inactiveProjects).toEqual([]);
		expect(get(measurementAgents).summariesKey).toBeNull();
		expect(get(measurementAgents).rows.length).toBe(1);
		expect(get(measurementAgents).findingsState).toBe('flagged');
		release();
		await pending;
		expect(get(measurementAgents).projectSummaries[0].outcomes).toBe(160);
		expect(get(measurementAgents).inactiveProjects).toEqual([]);
		expect(get(measurementAgents).summariesKey).toBe('all:none:weeks');
	});

	it('clears the project summaries with the rest of the surface when the scope changes', async () => {
		const served = () => empty({
			project_summaries: [{ project: { id: 'p1', name: 'Alpha' }, flagged_agents: 0, outcomes: 12 }],
			inactive_projects: [{ id: 'p2', name: 'Beta' }],
		});
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => served() })));
		await loadMeasurementAgents();
		expect(get(measurementAgents).projectSummaries.length).toBe(1);

		measurementScope.set({ project: 'project-1', agent: null });
		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async () => { await gate; return { ok: true, json: async () => empty() }; }));
		const pending = loadMeasurementAgents();
		expect(get(measurementAgents).projectSummaries).toEqual([]);
		expect(get(measurementAgents).inactiveProjects).toEqual([]);
		expect(get(measurementAgents).summariesKey).toBeNull();
		release();
		await pending;
	});

	it('discards the summaries of a response for a grain the reader already left', async () => {
		const forRange = (grain: string) => empty({
			period: { grain },
			project_summaries: [{ project: { id: 'p1', name: 'Alpha' }, flagged_agents: 0, outcomes: grain === 'days' ? 270 : 160 }],
		});
		let release: () => void = () => {};
		const gate = new Promise<void>((resolve) => { release = resolve; });
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			// The days answer is the slow one; the reader has moved to weeks by the time it lands.
			if (String(url).includes('grain=days')) await gate;
			return { ok: true, json: async () => forRange(new URL(url, 'http://x').searchParams.get('grain') ?? 'days') };
		}));
		const stale = loadMeasurementAgents('days');
		await Promise.resolve();
		measurementGrain.set('weeks');
		await loadMeasurementAgents('weeks');
		release();
		await stale;
		expect(get(measurementAgents).projectSummaries[0].outcomes).toBe(160);
		expect(get(measurementAgents).summariesKey).toBe('all:none:weeks');
	});

	it('keeps the summaries that answered for this very selection across a failed reload', async () => {
		const served = () => empty({
			project_summaries: [{ project: { id: 'p1', name: 'Alpha' }, flagged_agents: 0, outcomes: 160 }],
			inactive_projects: [{ id: 'p2', name: 'Beta' }],
		});
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => served() })));
		await loadMeasurementAgents();
		expect(get(measurementAgents).summariesKey).toBe('all:none:days');

		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })));
		await loadMeasurementAgents();
		// The retry asks for the same scope and the same grain, so the counts on screen
		// still describe what the reader is looking at, alongside the error and its retry.
		expect(get(measurementAgents).error).toBe('Could not load the agents table.');
		expect(get(measurementAgents).projectSummaries[0].outcomes).toBe(160);
		expect(get(measurementAgents).summariesKey).toBe('all:none:days');
	});

	it('drops the order claim on a failed reload but keeps what the scope already answered', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => empty({ rows: [row(idle)] }) })));
		await loadMeasurementAgents();
		expect(get(measurementAgents).rows.length).toBe(1);
		expect(get(measurementAgents).sort).toBe('ranked');

		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })));
		await loadMeasurementAgents();
		// A failed load cannot support a row or an order claim, so both go. The scope's
		// own automatic findings cannot have changed, so the retry keeps answering with
		// them instead of leaving the whole surface blank.
		expect(get(measurementAgents).error).toBe('Could not load the agents table.');
		expect(get(measurementAgents).rows).toEqual([]);
		expect(get(measurementAgents).sort).toBeNull();
		expect(get(measurementAgents).findingsState).toBe('healthy');
	});
});
