/**
 * The Agents response contract, checked against the producer.
 *
 * Positives are real producer bands: a fully measured agent, a thin agent that was
 * never compared, an eligible rule that did not flag, a valid empty scope and the
 * nulls the registry actually serves. Negatives are answers that HTTP `200` but cannot
 * support the claims the surface makes; each names the false claim it used to produce.
 */

import { describe, expect, it } from 'vitest';
import { decodeAgentsResponse } from './measurementAgentsContract';
import { agentsColumns, type AgentsRequestContract } from './measurementAgentsTypes';
import {
	servedBody,
	servedCell,
	servedEmptyScope,
	servedFinding,
	servedMetrics,
	servedRow,
	servedSummary,
	servedThinRow,
	servedThinRule,
	servedPoint,
} from '../__tests__/fixtures/measurementAgents';

const request: AgentsRequestContract = { project: 'project-1', agent: 'agent-1', metric: 'fresh_tokens_per_outcome', sort: 'ranked', grain: 'days' };

/** The ids the Agents table displays, read from the consumer's own column definition. */
const AGENTS_COLUMN_IDS = agentsColumns.map((column) => column.id);

/** Strip one served key, the way a partial serialization would. */
const withoutKey = (key: string) => {
	const body = servedBody(request);
	delete body[key];
	return body;
};

const rejects = (body: unknown, where: string) => {
	expect(() => decodeAgentsResponse(body, request), JSON.stringify(body)).toThrowError(new RegExp(where.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
};

describe('Agents response contract: what the producer actually serves', () => {
	it('accepts a fully measured agent with its four columns and order claim', () => {
		const served = decodeAgentsResponse(servedBody(request), request);
		expect(served.rows).toHaveLength(1);
		expect(served.findingsState).toBe('healthy');
		expect(served.grain).toBe('days');
		expect(served.sort).toBe('ranked');
	});

	it('accepts the thin rule of an agent that was never compared', () => {
		// _worsening answers {eligible:false, reason, metric, minimum_n} and nothing else:
		// no flagged, no window, no statistics. That absence is the honest answer.
		const served = decodeAgentsResponse(servedBody(request, { rows: [servedThinRow(request)] }), request);
		expect((served.rows[0] as ReturnType<typeof servedThinRow>).worsening.eligible).toBe(false);
		expect(JSON.stringify(served.rows[0])).not.toContain('flagged');
		expect(served.rows[0].rank).toBeNull();
		expect(served.rows[0].status).toBe('too_few');
	});

	it('accepts an eligible rule that found nothing adverse', () => {
		const row = servedRow(request, { worsening: { eligible: true, metric: request.metric, minimum_n: 20, flagged: false } });
		expect(decodeAgentsResponse(servedBody(request, { rows: [row] }), request).rows).toHaveLength(1);
	});

	it('accepts a valid empty scope and reports it as served, not as a failure', () => {
		const served = decodeAgentsResponse(servedEmptyScope(request), request);
		expect(served.findingsState).toBe('no_agents');
		expect(served.rows).toEqual([]);
		expect(served.findings).toEqual([]);
		expect(served.projectSummaries).toEqual([]);
	});

	it('keeps every served null a null', () => {
		// These are the real nullable distinctions: not measured, not ranked, no baseline
		// deviation to claim, and the All-work identity.
		const row = servedRow(request, {
			rank: null,
			metrics: servedMetrics(null),
			agent: { id: 'agent-9', name: 'Atlas', project: { id: null, name: null } },
		});
		const finding = servedFinding({ standardized_size: null, relative_adverse_change: 0.4 });
		const served = decodeAgentsResponse(servedBody(request, { rows: [row], findings: [finding], findings_state: 'too_little_history' }), request);
		expect(served.rows[0].rank).toBeNull();
		expect(served.rows[0].metrics.outcomes.value).toBeNull();
		expect(served.rows[0].agent.project.id).toBeNull();
		expect(served.findings[0].standardized_size).toBeNull();
		expect(served.findings[0].relative_adverse_change).toBe(0.4);
	});

	it('hands back the bytes that were served rather than rebuilt ones', () => {
		const body = servedBody(request);
		const [firstRow] = body.rows as unknown[];
		expect(decodeAgentsResponse(body, request).rows[0]).toBe(firstRow);
	});
});

describe('Agents response contract: a 200 that cannot support the surface', () => {
	it('rejects a missing rows list, which used to render the empty-scope headline', () => {
		rejects(withoutKey('rows'), 'rows is undefined');
	});

	it('rejects a missing findings list and a missing findings state', () => {
		rejects(withoutKey('findings'), 'findings is undefined');
		rejects(withoutKey('findings_state'), 'findings_state is undefined');
	});

	it('rejects a missing project list, which used to drop a project from PL-4 silently', () => {
		rejects(withoutKey('project_summaries'), 'project_summaries is undefined');
		rejects(withoutKey('inactive_projects'), 'inactive_projects is undefined');
	});

	it('rejects a missing caption and a missing order echo', () => {
		rejects(withoutKey('caption'), 'caption is undefined');
		rejects(withoutKey('sort'), 'sort is undefined');
	});

	it('rejects an answer that is not an object at all', () => {
		rejects(null, 'response is null');
		rejects('400 Bad Gateway', 'response is "400 Bad Gateway"');
	});

	it('rejects a list whose member is null or empty', () => {
		rejects(servedBody(request, { rows: [null] }), 'rows[0] is null');
		rejects(servedBody(request, { findings: [{}] }), 'findings[0].agent is undefined');
		rejects(servedBody(request, { project_summaries: [null] }), 'project_summaries[0] is null');
		rejects(servedBody(request, { inactive_projects: [{}] }), 'inactive_projects[0].id is undefined');
	});

	it('rejects an identity that is not an identity', () => {
		rejects(servedBody(request, { rows: [servedRow(request, { agent: { id: '', name: 'x', project: { id: null, name: null } } })] }), 'agent.id is ""');
		rejects(servedBody(request, { rows: [servedRow(request, { agent: { id: 'a', name: 'x' } })] }), 'agent.project is absent');
	});

	it('rejects a state the producer never decides', () => {
		rejects(servedBody(request, { findings_state: 'ok' }), 'findings_state is "ok"');
		rejects(servedBody(request, { rows: [servedRow(request, { status: 'weird' })] }), 'status is "weird"');
	});

	it('rejects bytes that answer a different question than the one asked', () => {
		rejects(servedBody(request, { period: { grain: 'weeks' } }), 'grain is "weeks", not the requested days');
		rejects(servedBody(request, { sort: 'worsening' }), 'sort is "worsening", not the requested ranked');
		rejects(servedBody(request, { metric: 'outcomes' }), 'metric is "outcomes", not the requested fresh_tokens_per_outcome');
		rejects(servedBody(request, { scope: { project: 'project-2', agent: 'agent-1' } }), 'scope.project is "project-2"');
		rejects(servedBody(request, { scope: { project: 'project-1', agent: null } }), 'scope.agent is null');
		rejects(servedBody(request, { rows: [servedRow(request, { selected_metric: 'outcomes' })] }), 'selected_metric is "outcomes"');
	});

	it('rejects a number that is not a finite number, and a rank that is not a position', () => {
		rejects(servedBody(request, { findings: [servedFinding({ standardized_size: Number.NaN })] }), 'standardized_size is NaN');
		rejects(servedBody(request, { findings: [servedFinding({ standardized_size: '4' })] }), 'standardized_size is "4"');
		rejects(servedBody(request, { findings: [servedFinding({ relative_adverse_change: Number.POSITIVE_INFINITY })] }), 'relative_adverse_change is Infinity');
		rejects(servedBody(request, { rows: [servedRow(request, { n: Number.POSITIVE_INFINITY })] }), 'n is Infinity');
		rejects(servedBody(request, { rows: [servedRow(request, { rank: 1.5 })] }), 'rank is 1.5');
		rejects(servedBody(request, { rows: [servedRow(request, { rank: '1' })] }), 'rank is "1"');
		rejects(servedBody(request, { rows: [servedRow(request, { difficulty_mix: { hard: '8' } })] }), 'difficulty_mix.hard is "8"');
	});

	it('rejects a cell or week that cannot be labelled, without re-computing anything', () => {
		rejects(servedBody(request, { rows: [servedRow(request, { metrics: { ...servedMetrics(), outcomes: undefined as unknown as never } })] }), 'metrics.outcomes is undefined');
		rejects(servedBody(request, { rows: [servedRow(request, { metrics: servedMetrics().fresh_tokens_per_outcome && { ...servedMetrics(), fresh_tokens_per_outcome: { ...servedCell(), periods: [null] } } })] }), 'periods[0] is null');
		rejects(servedBody(request, { rows: [servedRow(request, { metrics: { ...servedMetrics(), asks_per_outcome: { ...servedCell(), minimum_n: -1 } } })] }), 'minimum_n is -1');
		rejects(servedBody(request, { rows: [servedRow(request, { metrics: { ...servedMetrics(), outcomes: { ...servedCell(), periods: [servedPoint('2026-09-14', 1, 30, 20), { period: '', value: null, n: 0, minimum_n: 0 }] } } })] }), 'periods[1].period is ""');
	});

	it('rejects a rule that claims a comparison without answering it, and accepts the thin rule that does not', () => {
		rejects(servedBody(request, { rows: [servedRow(request, { worsening: { eligible: true, metric: request.metric, minimum_n: 20 } })] }), 'claims a comparison happened');
		// Both halves of the rule are questions the surface renders as yes or no. A string
		// that merely looks true would be printed as one, so it is refused, not coerced.
		rejects(servedBody(request, { rows: [servedRow(request, { worsening: { eligible: 'true', metric: request.metric, minimum_n: 20, flagged: true } })] }), 'worsening.eligible is "true", not a boolean');
		rejects(servedBody(request, { rows: [servedRow(request, { worsening: { eligible: true, metric: request.metric, minimum_n: 20, flagged: 1 } })] }), 'worsening.flagged is 1, not a boolean');
		// The producer's thin answer is eligible:false plus a reason the panel does not read:
		// its lack of flagged, window and statistics is legitimate, never a failure.
		const thin = servedBody(request, { rows: [servedRow(request, { worsening: servedThinRule(request.metric, 'not_measured'), flags: { fresh_tokens_per_outcome: { eligible: false } } })] });
		expect(decodeAgentsResponse(thin, request).rows).toHaveLength(1);
	});

	it('rejects a row that does not carry the column it was served for', () => {
		// The table reads `row.metrics[column.id]` and prints `.value`, so a row without
		// the cell for the column it was served for cannot be labelled at all — a missing
		// key is not a zero and not a dash the reader chose.
		const without = { ...servedMetrics() } as Record<string, unknown>;
		delete without[request.metric];
		rejects(servedBody(request, { rows: [servedRow(request, { metrics: without })] }), 'metrics has no cell for the requested');
	});

	it.each(AGENTS_COLUMN_IDS.filter((id) => id !== request.metric))(
		'rejects a row missing the displayed column %s, which the table would call Unmeasured',
		(id) => {
			// The metric PICKER offers every `agentsColumns` entry and the body renders all
			// of them at MeasurementAgentsTable.svelte:124-128, treating `cell === undefined`
			// and a served null identically as the word "Unmeasured". So a row that omits an
			// UNSELECTED column still passes a selected-only check and then states a
			// measurement status the producer never supplied. Required ids come from the
			// consumer's own column definition, not from a second list kept by hand.
			const without = { ...servedMetrics() } as Record<string, unknown>;
			delete without[id];
			rejects(servedBody(request, { rows: [servedRow(request, { metrics: without })] }), `metrics has no cell for the displayed column ${id}`);
		},
	);

	it('accepts every displayed column served explicitly null, which is a thin answer and not an absence', () => {
		// The distinction the table cannot make on its own: `{value: null}` is "the producer
		// measured this and had nothing to say", an absent key is "never answered". Only the
		// second is a failed load, so the first must keep passing for every column.
		for (const id of AGENTS_COLUMN_IDS) {
			const nulled = { ...servedMetrics() } as Record<string, unknown>;
			nulled[id] = servedCell(null);
			const served = decodeAgentsResponse(servedBody(request, { rows: [servedRow(request, { metrics: nulled })] }), request);
			expect((served.rows[0] as { metrics: Record<string, { value: number | null }> }).metrics[id].value).toBeNull();
		}
	});

	it('rejects a project list that cannot be counted, in either direction', () => {
		// PL-4 renders these two numbers as counts of agents and outcomes; a string, a
		// negative or a non-finite value would read as a measurement of something.
		rejects(servedBody(request, { project_summaries: [servedSummary({ flagged_agents: 'one' })] }), 'flagged_agents is "one"');
		rejects(servedBody(request, { project_summaries: [servedSummary({ outcomes: -3 })] }), 'outcomes is -3');
		rejects(servedBody(request, { inactive_projects: [{ id: 'project-2', name: '' }] }), 'inactive_projects[0].name is ""');
	});

	it('rejects a contract this surface does not consume', () => {
		rejects(servedBody(request, { definition_version: 2 }), 'definition_version is 2');
		rejects(servedBody(request, { definition_version: undefined }), 'definition_version is undefined');
	});
});
