/**
 * What the Agents surface does with the bytes it is handed.
 *
 * This is the behaviour the BLOCK was about: a `200` whose body cannot support an empty
 * state, an order claim or a findings verdict must be reported as a failed load, and the
 * only path that may produce that outcome is the store's existing one. Nothing here
 * rebuilds state on a new idea: the assertions are the retained-bytes rules the surface
 * already follows (scope-bound findings survive a failed load, rows and their order claim
 * do not, the lists stay keyed to the grain they were counted for).
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { measurementScope } from './measurement';
import { measurementGrain } from './measurementGrain';
import { loadMeasurementAgents, measurementAgents, measurementAgentsView } from './measurementAgents';
import type { AgentsRequestContract } from '../api/measurementAgentsTypes';
import { servedBody, servedEmptyScope, servedFinding, servedRow, servedThinRow } from '../__tests__/fixtures/measurementAgents';
import { token } from './auth';

const REQUEST = { project: 'project-1', agent: 'agent-1', metric: 'fresh_tokens_per_outcome', sort: 'ranked', grain: 'days' } as const satisfies AgentsRequestContract;

function serve(body: unknown) {
	return vi.fn(async () => ({ ok: true, json: async () => body }));
}

async function load(request = REQUEST) {
	token.set('test-token');
	measurementScope.set({ project: request.project, agent: request.agent });
	measurementAgentsView.set({ metric: request.metric, sort: request.sort });
	measurementGrain.set(request.grain);
	await loadMeasurementAgents();
	return get(measurementAgents);
}

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	token.set(null);
	measurementGrain.set('days');
	measurementScope.set({ project: 'all', agent: null });
	measurementAgentsView.set({ metric: 'fresh_tokens_per_outcome', sort: 'ranked' });
	measurementAgents.set({ loading: false, error: null, rows: [], findings: [], findingsState: null, caption: '', selection: '', scopeKey: '', servedView: null, grain: null, sort: null, projectSummaries: [], inactiveProjects: [], summariesKey: null });
});

describe('a 200 that cannot support the surface is a failed load', () => {
	it('does not answer "No active agents for this selection." for a body with no rows list', async () => {
		const body = servedBody(REQUEST);
		delete body.rows;
		vi.stubGlobal('fetch', serve(body));
		const state = await load();
		expect(state.error).toBe('Could not load the agents table.');
		expect(state.rows).toEqual([]);
		expect(state.loading).toBe(false);
	});

	it('does not keep an order claim for bytes whose echo names a different sort', async () => {
		vi.stubGlobal('fetch', serve(servedBody(REQUEST, { rows: [servedRow(REQUEST)], sort: 'worsening' })));
		const state = await load();
		expect(state.error).toBe('Could not load the agents table.');
		expect(state.sort).toBeNull();
		expect(state.rows).toEqual([]);
	});

	it('does not key project lists to a grain the response never answered for', async () => {
		vi.stubGlobal('fetch', serve(servedBody(REQUEST, { period: { grain: 'weeks' } })));
		const state = await load();
		expect(state.error).toBe('Could not load the agents table.');
		expect(state.summariesKey).toBeNull();
		expect(state.projectSummaries).toEqual([]);
	});

	it('does not treat a null row member as an agent', async () => {
		vi.stubGlobal('fetch', serve(servedBody(REQUEST, { rows: [null] })));
		expect((await load()).error).toBe('Could not load the agents table.');
	});

	it('does not claim a findings state the producer never decided, and keeps what the scope already answered', async () => {
		// First a served scope, then the broken answer: the automatic findings are the one
		// thing a failed reload cannot have changed.
		vi.stubGlobal('fetch', serve(servedBody(REQUEST, { findings: [servedFinding()], findings_state: 'flagged' })));
		await load();
		const before = get(measurementAgents);
		expect(before.findingsState).toBe('flagged');
		const broken = servedBody(REQUEST, { findings_state: 'ok' });
		vi.stubGlobal('fetch', serve(broken));
		await loadMeasurementAgents();
		const state = get(measurementAgents);
		expect(state.error).toBe('Could not load the agents table.');
		expect(state.findingsState).toBe('flagged');
		expect(state.findings).toHaveLength(1);
		expect(state.rows).toEqual([]);
		expect(state.sort).toBeNull();
	});

	it('does not accept bytes that belong to another scope', async () => {
		vi.stubGlobal('fetch', serve(servedBody(REQUEST, { scope: { project: 'project-2', agent: 'agent-2' } })));
		expect((await load()).error).toBe('Could not load the agents table.');
	});

	it('offers the retry the failed-load path already provides', async () => {
		vi.stubGlobal('fetch', serve(servedBody(REQUEST, { findings: [servedFinding({ standardized_size: Number.NaN })] })));
		expect((await load()).error).toBe('Could not load the agents table.');
		vi.stubGlobal('fetch', serve(servedBody(REQUEST)));
		await loadMeasurementAgents();
		const state = get(measurementAgents);
		expect(state.error).toBeNull();
		expect(state.rows).toHaveLength(1);
	});
});

describe('what the producer really serves stays served, not repaired', () => {
	it('serves a valid empty scope as the scope headline, with no error', async () => {
		vi.stubGlobal('fetch', serve(servedEmptyScope(REQUEST)));
		const state = await load();
		expect(state.error).toBeNull();
		expect(state.findingsState).toBe('no_agents');
		expect(state.rows).toEqual([]);
	});

	it('serves a thin agent that was never compared, with its nulls intact', async () => {
		vi.stubGlobal('fetch', serve(servedBody(REQUEST, { rows: [servedThinRow(REQUEST)], findings_state: 'too_little_history' })));
		const state = await load();
		expect(state.error).toBeNull();
		expect(state.rows[0].rank).toBeNull();
		expect(state.rows[0].status).toBe('too_few');
		expect(state.findingsState).toBe('too_little_history');
	});

	it('serves a measured row and the order it was served in', async () => {
		vi.stubGlobal('fetch', serve(servedBody(REQUEST)));
		const state = await load();
		expect(state.error).toBeNull();
		expect(state.sort).toBe('ranked');
		expect(state.grain).toBe('days');
		expect(state.summariesKey).toBe('project-1:agent-1:days');
		expect(state.projectSummaries).toHaveLength(1);
	});
});
