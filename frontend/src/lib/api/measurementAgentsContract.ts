/**
 * The consumed contract of `GET /api/measurement/agents`, checked.
 *
 * A `200` is not a proof of content. The Agents surface makes claims that only hold
 * if the bytes that produced them carry them — "No active agents in this scope."
 * claims an empty population, an `aria-sort` claim describes the order these very
 * rows are in, the needs-a-look line claims what the automatic findings found.
 * Before this decoder a missing field was quietly replaced by an empty list or a
 * `null`, so a broken answer rendered as a finished measurement.
 *
 * What is checked is what a consumer dereferences, and nothing more: the table
 * (`MeasurementAgentsTable.svelte`), the automatic findings the
 * store keeps, the keys the store keeps its surviving bytes
 * under, and the PL-4 project lists this endpoint serves for the picker that
 * follows. Because the table renders every column in the consumer's own `agentsColumns`
 * list — not only the metric the reader selected — a row is complete against that list or
 * it is not renderable: a cell the producer never answered would otherwise be printed as
 * `Unmeasured`, a measurement status that was never served. Statistics are never recomputed, no length or ordering is asserted beyond
 * what a consumer reads, and nothing is defaulted, coerced or repaired — the bytes
 * that were served are the bytes that are returned. An answer that cannot support
 * the claims throws, and the store's existing failed-load path owns what the reader
 * then sees.
 */

import type {
	AgentMetric,
	AgentRule,
	AgentsInactiveProject,
	AgentsProjectSummary,
	AgentsRequestContract,
	FindingsState,
	MeasurementAgentRow,
	MeasurementFinding,
} from './measurementAgentsTypes';
import { decodePeriod, type MeasurementPeriod } from '../stores/measurementPeriod';
import { AGENT_STATUSES, FINDINGS_STATES, agentsColumns } from './measurementAgentsTypes';

/** The served answer, checked and passed through unchanged. */
export interface AgentsServed {
	readonly rows: MeasurementAgentRow[];
	readonly findings: MeasurementFinding[];
	readonly findingsState: FindingsState;
	readonly caption: string;
	readonly sort: string;
	readonly grain: string;
	/** The echoed windows, or null when the response did not carry a usable pair (the table then names no window). */
	readonly period: MeasurementPeriod | null;
	readonly projectSummaries: AgentsProjectSummary[];
	readonly inactiveProjects: AgentsInactiveProject[];
}

function fail(what: string): never {
	throw new Error(`agents response: ${what}`);
}

function describe(value: unknown): string {
	if (value === null) return 'null';
	if (Array.isArray(value)) return 'an array';
	if (typeof value === 'object') return 'an object';
	if (typeof value === 'string') return JSON.stringify(value);
	return String(value);
}

function isRecord(value: unknown, where: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) fail(`${where} is ${describe(value)}, not an object`);
	return value as Record<string, unknown>;
}

function isList(value: unknown, where: string): readonly unknown[] {
	if (!Array.isArray(value)) fail(`${where} is ${describe(value)}, not an array`);
	return value as readonly unknown[];
}

function isText(value: unknown, where: string): string {
	if (typeof value !== 'string' || !value.length) fail(`${where} is ${describe(value)}, not a non-empty string`);
	return value;
}

function isBoolean(value: unknown, where: string): boolean {
	if (typeof value !== 'boolean') fail(`${where} is ${describe(value)}, not a boolean`);
	return value;
}

/** A number the surface may print, sort or compare: finite only, so `NaN` and `Infinity` never read as a measurement. */
function isFiniteNumber(value: unknown, where: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) fail(`${where} is ${describe(value)}, not a finite number`);
	return value;
}

function isCount(value: unknown, where: string): number {
	const number = isFiniteNumber(value, where);
	if (number < 0) fail(`${where} is ${number}, which cannot be a count`);
	return number;
}

/**
 * The served nullable distinction: a real number, or `null` meaning "the registry did
 * not measure this". A missing key is neither of those, and a string that happens to
 * look like a number is not what the formatters accept.
 */
function isNumberOrNull(value: unknown, where: string): number | null {
	if (value === null) return null;
	return isFiniteNumber(value, where);
}

function oneOf<T extends readonly string[]>(value: unknown, allowed: T, where: string): T[number] {
	if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) {
		fail(`${where} is ${describe(value)}, not one of ${allowed.join('|')}`);
	}
	return value as T[number];
}

function hasKey(value: Record<string, unknown>, key: string): boolean {
	return Object.hasOwn(value, key);
}

/** A rank is a position: an ordinal number, or `null` meaning "not ranked". A fraction is neither. */
function checkRank(value: unknown, where: string): void {
	const number = isFiniteNumber(value, where);
	if (!Number.isInteger(number) || number < 1) fail(`${where} is ${number}, not a rank position or null`);
}

/**
 * The agent identity a row and a finding both carry. `project: {id: null, name: null}`
 * is the served All-work identity, so null is checked for presence, not treated as absent.
 */
function checkAgent(value: unknown, where: string): void {
	const agent = isRecord(value, where);
	isText(agent.id, `${where}.id`);
	isText(agent.name, `${where}.name`);
	if (!hasKey(agent, 'project')) fail(`${where}.project is absent`);
	const project = isRecord(agent.project, `${where}.project`);
	if (project.id !== null) isText(project.id, `${where}.project.id`);
	if (project.name !== null) isText(project.name, `${where}.project.name`);
}

function checkPeriod(value: unknown, where: string): void {
	const period = isRecord(value, where);
	isText(period.period, `${where}.period`);
	isNumberOrNull(period.value, `${where}.value`);
	isCount(period.n, `${where}.n`);
	isCount(period.minimum_n, `${where}.minimum_n`);
}

/**
 * One metric cell: the pooled value and its weekly frame. The table indexes
 * `row.metrics[column.id]` and reads `.value`, so a cell that is present must carry a
 * real value-or-null and the counts its label depends on. Which columns exist is the
 * component's own list and its own test; this does not re-declare the registry's ids.
 */
function checkMetricCell(value: unknown, where: string): void {
	const cell = isRecord(value, where);
	isNumberOrNull(cell.value, `${where}.value`);
	isCount(cell.n, `${where}.n`);
	isCount(cell.minimum_n, `${where}.minimum_n`);
	if (hasKey(cell, 'previous_value')) isNumberOrNull(cell.previous_value, `${where}.previous_value`);
	if (hasKey(cell, 'previous_n')) isCount(cell.previous_n, `${where}.previous_n`);
	isList(cell.periods, `${where}.periods`).forEach((point, index) => checkPeriod(point, `${where}.periods[${index}]`));
}

/**
 * One automatic-finding rule for one agent.
 *
 * An agent that was never compared is served a thin rule — `eligible: false` with its
 * `reason`, `metric` and `minimum_n`, and deliberately no `flagged`, no window and no
 * statistics. That absence is the honest answer, so it is legal here. An eligible rule
 * claims a comparison happened, and the row marker reads `flagged`, so that one must
 * answer it. Nothing else on a rule is dereferenced, so nothing else is required.
 */
function checkRule(value: unknown, where: string): void {
	const rule = isRecord(value, where);
	const eligible = isBoolean(rule.eligible, `${where}.eligible`);
	if (hasKey(rule, 'flagged')) isBoolean(rule.flagged, `${where}.flagged`);
	else if (eligible) fail(`${where} claims a comparison happened without a flagged answer`);
}

function checkRow(value: unknown, where: string, request: AgentsRequestContract): void {
	const row = isRecord(value, where);
	checkAgent(row.agent, `${where}.agent`);
	oneOf(row.status, AGENT_STATUSES, `${where}.status`);
	if (row.rank !== null) checkRank(row.rank, `${where}.rank`);
	isCount(row.n, `${where}.n`);
	// The order claim is a fact about the column these rows were served for, not about
	// whichever control the reader last touched.
	if (row.selected_metric !== request.metric) fail(`${where}.selected_metric is ${describe(row.selected_metric)}, not the requested ${request.metric}`);
	for (const [name, count] of Object.entries(isRecord(row.difficulty_mix, `${where}.difficulty_mix`))) {
		isCount(count, `${where}.difficulty_mix.${name}`);
	}
	const metrics = isRecord(row.metrics, `${where}.metrics`);
	if (!hasKey(metrics, request.metric)) fail(`${where}.metrics has no cell for the requested ${request.metric}`);
	// Every column the consumer displays must be answered, not just the selected one: the
	// table body iterates `agentsColumns` and prints an absent cell with the same word it
	// uses for a served null. Required ids come from that definition, so a fifth column
	// cannot be added to the table without the check learning it. A present cell whose
	// `value` is null stays a legitimate thin answer; only the missing key is a failure.
	for (const column of agentsColumns) {
		if (column.id === request.metric) continue;
		if (!hasKey(metrics, column.id)) fail(`${where}.metrics has no cell for the displayed column ${column.id}`);
	}
	for (const [id, cell] of Object.entries(metrics)) checkMetricCell(cell, `${where}.metrics.${id}`);
	checkRule(row.worsening, `${where}.worsening`);
	for (const [name, rule] of Object.entries(isRecord(row.flags, `${where}.flags`))) checkRule(rule, `${where}.flags.${name}`);
}

/**
 * One automatic finding. `move()` formats `standardized_size`, then
 * `relative_adverse_change`, and falls back to a plain sentence only when both are
 * null, so a null must stay null and a non-finite number would be printed as a
 * measurement. A finding's metric is its own id and need not be the selected column.
 * The statistics the panel does not read are not re-checked.
 */
function checkFinding(value: unknown, where: string): void {
	const finding = isRecord(value, where);
	checkAgent(finding.agent, `${where}.agent`);
	isText(finding.metric, `${where}.metric`);
	isNumberOrNull(finding.standardized_size, `${where}.standardized_size`);
	isNumberOrNull(finding.relative_adverse_change, `${where}.relative_adverse_change`);
	isText(finding.action, `${where}.action`);
}

function checkSummary(value: unknown, where: string): void {
	const summary = isRecord(value, where);
	const project = isRecord(summary.project, `${where}.project`);
	isText(project.id, `${where}.project.id`);
	isText(project.name, `${where}.project.name`);
	isCount(summary.flagged_agents, `${where}.flagged_agents`);
	isCount(summary.outcomes, `${where}.outcomes`);
}

function checkInactiveProject(value: unknown, where: string): void {
	const project = isRecord(value, where);
	isText(project.id, `${where}.id`);
	isText(project.name, `${where}.name`);
}

function tryPeriod(value: unknown): MeasurementPeriod | null {
	try { return decodePeriod(value); } catch { return null; }
}

/**
 * Check an Agents response against the contract this surface consumes, and hand back
 * the same bytes.
 *
 * Any answer that cannot support those claims throws — including an echo naming a
 * different scope, grain, column or metric than the request, because those bytes would
 * otherwise be keyed under the current selection and look current while answering a
 * different question.
 */
export function decodeAgentsResponse(body: unknown, request: AgentsRequestContract): AgentsServed {
	const served = isRecord(body, 'response');
	if (served.definition_version !== 1) fail(`definition_version is ${describe(served.definition_version)}, not 1`);
	const scope = isRecord(served.scope, 'scope');
	if (scope.project !== request.project) fail(`scope.project is ${describe(scope.project)}, not the requested ${request.project}`);
	if (scope.agent !== request.agent) fail(`scope.agent is ${describe(scope.agent)}, not the requested ${describe(request.agent)}`);
	const grain = isText(isRecord(served.period, 'period').grain, 'period.grain');
	if (grain !== request.grain) fail(`grain is ${describe(grain)}, not the requested ${request.grain}`);
	const sort = isText(served.sort, 'sort');
	if (sort !== request.sort) fail(`sort is ${describe(sort)}, not the requested ${request.sort}`);
	const metric = isText(served.metric, 'metric');
	if (metric !== request.metric) fail(`metric is ${describe(metric)}, not the requested ${request.metric}`);
	const rows = isList(served.rows, 'rows');
	rows.forEach((row, index) => checkRow(row, `rows[${index}]`, request));
	const findings = isList(served.findings, 'findings');
	findings.forEach((finding, index) => checkFinding(finding, `findings[${index}]`));
	const findingsState = oneOf(served.findings_state, FINDINGS_STATES, 'findings_state');
	const caption = isText(served.caption, 'caption');
	const projectSummaries = isList(served.project_summaries, 'project_summaries');
	projectSummaries.forEach((summary, index) => checkSummary(summary, `project_summaries[${index}]`));
	const inactiveProjects = isList(served.inactive_projects, 'inactive_projects');
	inactiveProjects.forEach((project, index) => checkInactiveProject(project, `inactive_projects[${index}]`));
	// Passed through, not rebuilt: what the store keeps is exactly what was served.
	return {
		rows: rows as MeasurementAgentRow[],
		findings: findings as MeasurementFinding[],
		findingsState,
		caption,
		sort,
		grain,
		period: tryPeriod(served.period),
		projectSummaries: projectSummaries as AgentsProjectSummary[],
		inactiveProjects: inactiveProjects as AgentsInactiveProject[],
	};
}
