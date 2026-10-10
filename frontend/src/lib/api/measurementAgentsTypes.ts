/**
 * The vocabulary `GET /api/measurement/agents` serves, in one home.
 *
 * These are the shapes the Agents surface dereferences by name: the states it can label,
 * the request whose echoes decide what may be kept, and the row, rule, cell, finding and
 * project-list shapes the table and the needs-a-look panel read. The checker and the
 * decoder live in ``measurementAgentsContract.ts``; nothing here takes a RUNTIME dependency
 * on a store, so the dependency runs one way — vocabulary, then the checks that use it,
 * then the store. The one import below is type-only, erased at build, and
 * ``stores/measurement.ts`` imports nothing from here, so it cannot close a cycle.
 */

import type { MeasurementFormat } from '../stores/measurement';

/** The automatic-finding states the needs-a-look panel can name. One home: the store, the checker and the panel all read them here. */
export const FINDINGS_STATES = ['flagged', 'too_little_history', 'healthy', 'no_agents'] as const;
/** The ranking states a served row can be in, for the same reason. */
export const AGENT_STATUSES = ['eligible', 'too_few'] as const;

export type FindingsState = (typeof FINDINGS_STATES)[number];
export type AgentStatus = (typeof AGENT_STATUSES)[number];

/** The metric ids the Agents route serves and the table can display. One home: the store, the checker, the picker and the panel all read them here. */
export type AgentsMetric =
	| 'outcomes' | 'fresh_tokens_per_outcome' | 'first_try_acceptance' | 'asks_per_outcome'
	| 'active_hours_per_outcome' | 'dollars_per_work_hour';

/**
 * The columns the Agents table serves, in display order, with how each is rendered.
 *
 * This is the consumer's own definition, and it is what a row must be complete *for*:
 * `MeasurementAgentsTable.svelte` iterates this list in its header, its metric picker and
 * every body row, and prints an absent cell with the same word it uses for a served null
 * (`Unmeasured`). So the decoder's notion of a usable row is derived here rather than from
 * a shorter list kept beside it. Lives in the vocabulary module because the checks must read
 * it and the store must not be reachable from them: the dependency stays one way.
 */
export const agentsColumns: { id: AgentsMetric; label: string; format: MeasurementFormat; note: string }[] = [
	{ id: 'first_try_acceptance', label: 'First try', format: 'percent', note: 'Share of accepted work that took one try.' },
	{ id: 'active_hours_per_outcome', label: 'Change in minutes per outcome, vs the previous window', format: 'minutes',
	  note: 'Your minutes per outcome, against the weeks before.' },
];

/**
 * What the caller asked for. Each echo is compared against this rather than against
 * a constant list, because kept bytes are keyed by scope, grain, column and order:
 * bytes that answer a different question must never be stored under this one.
 */
export interface AgentsRequestContract {
	readonly project: string;
	readonly agent: string | null;
	readonly metric: string;
	readonly sort: string;
	readonly grain: string;
}

/**
 * One agent's automatic finding rule: comparable, or why it is not.
 *
 * An ineligible rule carries `reason` and none of the statistics, because there is
 * nothing to state; an eligible one answers `flagged`. That difference is the shape
 * the table's marker reads, so it belongs to the contract rather than to a comment.
 */
export interface AgentRule {
	eligible: boolean;
	metric: string;
	minimum_n: number;
	reason?: string;
	window?: { from: string; to: string };
	mean?: number;
	sd?: number;
	current?: number;
	adverse_size?: number | null;
	zero_variance_baseline?: boolean;
	relative_adverse_change?: number | null;
	flagged?: boolean;
}

/** One served Agents metric: the pooled cell, its population, and the weekly frame. */
export interface AgentMetric {
	value: number | null;
	n: number;
	minimum_n: number;
	periods: { period: string; value: number | null; n: number; minimum_n: number }[];
	/** Served with minutes per outcome and dollars per work hour: the same pooled value for the previous window. */
	previous_value?: number | null;
	previous_n?: number;
}

/** One measured agent's row. */
export interface MeasurementAgentRow {
	agent: { id: string; name: string; project: { id: string | null; name: string | null } };
	n: number;
	difficulty_mix: Record<string, number>;
	status: AgentStatus;
	/** The human minutes spent on this agent in the latest window; null or absent when unmeasured. */
	active_minutes?: number | null;
	rank: number | null;
	selected_metric: string;
	metrics: Record<string, AgentMetric>;
	worsening: AgentRule;
	flags: Record<string, AgentRule>;
}

/** One automatic finding: the move, its statistics and the prescribed action. */
export interface MeasurementFinding {
	agent: { id: string; name: string; project: { id: string | null; name: string | null } };
	metric: string;
	current: number;
	mean: number;
	sd: number;
	n: number;
	standardized_size: number | null;
	zero_variance_baseline: boolean;
	relative_adverse_change: number | null;
	minimum_n: number;
	window: { from: string; to: string };
	action: string;
}

/**
 * One project counted over the selected grain: what its agents delivered
 * there and how many of them the automatic findings flag. This is the only part of
 * the Agents answer that moves with the grain, so it travels with the grain it
 * was counted for and is never presented on its own.
 */
export interface AgentsProjectSummary {
	project: { id: string; name: string };
	flagged_agents: number;
	outcomes: number;
}

/** A project whose agents served no outcome in the served grain. */
export interface AgentsInactiveProject {
	id: string;
	name: string;
}
