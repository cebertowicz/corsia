/**
 * Test support for the Agents contract: canonical complete served bodies.
 *
 * The route (`backend/api/measurement.py`, `measurement_agents()`) always answers with
 * all of `definition_version`, `scope`, `sort`, `metric`, `grain`, `caption`, `rows`,
 * `findings`, `findings_state`, `project_summaries` and `inactive_projects`, and the
 * echoes name the request that produced them. Fixtures here bind those echoes from the
 * caller's request, so a test that changes the grain, column, order or scope cannot
 * leave a body describing the old one behind.
 *
 * Every producer band the surface has to render is a named builder, not an ad-hoc
 * object: a fully measured row, a thin row for an agent that was never compared, an
 * eligible rule that did not flag, a valid empty scope, and a scope with findings.
 */

import type { AgentsRequestContract } from '../../api/measurementAgentsTypes';

/** The echoed windows: the last full seven (weeks) or one (days) and the window before, `to` inclusive. */
export function servedPeriod(grain: string) {
	return grain === 'days'
		? { grain, timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-09-22', to: '2026-10-04' },
			latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } }
		: { grain, timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-08-10', to: '2026-10-04' },
			latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } };
}

/** One measured week cell as the registry serves it. */
export function servedPoint(period = '2026-09-14', value: number | null = 12, n = 30, minimum_n = 20) {
	return { period, value, n, minimum_n };
}

/** One metric cell: pooled value, its population and the weekly frame. */
export function servedCell(value: number | null = 12, n = 30, minimum_n = 20, periods = [servedPoint()]) {
	return { value, n, minimum_n, periods };
}

/** The cells the table indexes by column id. */
export function servedMetrics(value: number | null = 12) {
	return {
		outcomes: servedCell(value),
		fresh_tokens_per_outcome: servedCell(value),
		first_try_acceptance: servedCell(value),
		asks_per_outcome: servedCell(value),
		active_hours_per_outcome: { ...servedCell(value), previous_value: value, previous_n: 30 },
		dollars_per_work_hour: { ...servedCell(value), previous_value: value, previous_n: 30 },
	};
}

/** An eligible rule: the comparison happened, so it answers `flagged`. */
export function servedEligibleRule(metric: string, flagged: boolean) {
	return {
		eligible: true,
		metric,
		minimum_n: 20,
		window: { from: '2026-08-03', to: '2026-09-14' },
		mean: 10,
		sd: 1,
		current: 14,
		adverse_size: flagged ? 4 : null,
		zero_variance_baseline: false,
		relative_adverse_change: null,
		flagged,
	};
}

/**
 * The thin rule an ineligible agent is served: `eligible: false` with its reason,
 * metric and threshold, and deliberately no `flagged`, no window and no statistics.
 * This is the producer's real shape at `measurement_agents.py:196-201`, not a test
 * convenience, and a contract check must accept it.
 */
export function servedThinRule(metric: string, reason = 'too_little_history') {
	return { eligible: false, reason, metric, minimum_n: 20 };
}

/** One fully measured agent row. */
export function servedRow(request: AgentsRequestContract, overrides: Record<string, unknown> = {}) {
	return {
		agent: { id: 'agent-1', name: 'Atlas', project: { id: 'project-1', name: 'CORSIA' } },
		n: 30,
		active_minutes: 120,
		difficulty_mix: { hard: 8, routine: 22 },
		status: 'eligible',
		rank: 1,
		selected_metric: request.metric,
		metrics: servedMetrics(),
		worsening: servedEligibleRule(request.metric, false),
		flags: { fresh_tokens_per_outcome: servedThinRule('fresh_tokens_per_outcome'), first_try_acceptance: servedThinRule('first_try_acceptance') },
		...overrides,
	};
}

/** A row for an agent that was never compared: too few outcomes, no rank, thin rules. */
export function servedThinRow(request: AgentsRequestContract) {
	return servedRow(request, {
		n: 4,
		status: 'too_few',
		rank: null,
		metrics: servedMetrics(null),
		worsening: servedThinRule(request.metric, 'not_measured'),
	});
}

/** One automatic finding, as the scope-level panel serves it. */
export function servedFinding(overrides: Record<string, unknown> = {}) {
	return {
		agent: { id: 'agent-1', name: 'Atlas', project: { id: 'project-1', name: 'CORSIA' } },
		metric: 'fresh_tokens_per_outcome',
		current: 14,
		mean: 10,
		sd: 1,
		n: 30,
		standardized_size: 4,
		zero_variance_baseline: false,
		relative_adverse_change: null,
		minimum_n: 20,
		window: { from: '2026-08-03', to: '2026-09-14' },
		action: 'Review its 5 costliest recent tasks.',
		...overrides,
	};
}

export function servedSummary(overrides: Record<string, unknown> = {}) {
	return { project: { id: 'project-1', name: 'CORSIA' }, flagged_agents: 1, outcomes: 30, ...overrides };
}

/**
 * A complete served body for one request, with every echo bound to it.
 *
 * A `rows: []` answer with `findings_state: 'no_agents'` is a valid empty scope rather
 * than a missing field, so the empty builders pass the same check the populated ones do.
 */
export function servedBody(request: AgentsRequestContract, overrides: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		definition_version: 1,
		scope: { project: request.project, agent: request.agent },
		sort: request.sort,
		metric: request.metric,
		period: servedPeriod(request.grain),
		caption: 'Trailing 8 completed weeks',
		rows: [servedRow(request)],
		findings: [],
		findings_state: 'healthy',
		project_summaries: [servedSummary()],
		inactive_projects: [{ id: 'project-2', name: 'Second project' }],
		...overrides,
	};
}

/** The honest empty scope: no agents, so nothing was compared. */
export function servedEmptyScope(request: AgentsRequestContract) {
	return servedBody(request, { rows: [], findings: [], findings_state: 'no_agents', project_summaries: [], inactive_projects: [] });
}
