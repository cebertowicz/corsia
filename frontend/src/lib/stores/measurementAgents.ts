/**
 * The Agents surface: the rows, findings and order state the Agents table serves.
 *
 * This is the Agents half of the measurement store, kept apart from the trend
 * panels because it answers a different question — what each agent is doing on its
 * own baseline — and it changes for reasons the panels do not. The composite scope
 * stays in `measurement.ts` so one surface still owns project and agent together.
 */

import { get, writable } from 'svelte/store';
import { decodeAgentsResponse } from '../api/measurementAgentsContract';
import type {
	AgentsInactiveProject,
	AgentsMetric,
	AgentsProjectSummary,
	FindingsState,
	MeasurementAgentRow,
	MeasurementFinding,
} from '../api/measurementAgentsTypes';
import type { MeasurementPeriod } from './measurementPeriod';
import { measurementScope, type MeasurementScope } from './measurement';
import { grainScopeParams, measurementGrain, type MeasurementGrain } from './measurementGrain';

/**
 * The Agents wire vocabulary lives in ``api/measurementAgentsTypes.ts``, the one home for
 * what the route serves, and is re-exported here unchanged so the surface keeps one import
 * path while one module owns the vocabulary and one owns the checks against it.
 */
export type {
	AgentMetric,
	AgentRule,
	AgentsInactiveProject,
	AgentsMetric,
	AgentsProjectSummary,
	MeasurementAgentRow,
	MeasurementFinding,
} from '../api/measurementAgentsTypes';

/** Re-exported unchanged: the table renders these columns and the decoder requires them, from the one definition. */
export { agentsColumns } from '../api/measurementAgentsTypes';

export interface AgentsState {
	loading: boolean;
	error: string | null;
	rows: MeasurementAgentRow[];
	findings: MeasurementFinding[];
	findingsState: FindingsState | null;
	caption: string;
	/** The grain, scope, metric and sort these bytes were loaded for. */
	selection: string;
	/** The composite scope the kept findings and rows answer for. */
	scopeKey: string;
	/** The column and order the kept rows were served under, or null after a clear. */
	servedView: string | null;
	grain: MeasurementGrain | null;
	/** The windows these rows answer for, or null before they load or when none was served. */
	period?: MeasurementPeriod | null;
	/**
	 * The sort these very rows were served under. The order a column may claim in
	 * ``aria-sort`` is a fact about the served rows, not about whichever control the
	 * reader is dragging, so the table reads it from the response echo.
	 */
	sort: AgentsSort | null;
	/** The project summaries counted for ``summariesKey``, empty until they answer for it. */
	projectSummaries: AgentsProjectSummary[];
	/** The projects with no outcome in that same served scope and grain. */
	inactiveProjects: AgentsInactiveProject[];
	/**
	 * The composite scope and served grain those two lists were counted for, or null
	 * when nothing on screen answers for the current selection. The lists are only
	 * ever current together with this key: a grain change clears them rather than
	 * leaving the old grain's counts labeled as the new one.
	 */
	summariesKey: string | null;
}

export type AgentsSort = 'ranked' | 'worsening';

export const measurementAgentsView = writable<{ metric: AgentsMetric; sort: AgentsSort }>({
	metric: 'fresh_tokens_per_outcome',
	sort: 'ranked',
});
export const measurementAgents = writable<AgentsState>({
	loading: true, error: null, rows: [], findings: [], findingsState: null, caption: '',
	selection: '', scopeKey: '', servedView: null, grain: null, sort: null,
	projectSummaries: [], inactiveProjects: [], summariesKey: null,
});

const controllers = new Map<string, AbortController>();

function agentsScopeKey(scope: MeasurementScope): string {
	return `${scope.project}:${scope.agent ?? 'none'}`;
}

function agentsViewKey(view: { metric: AgentsMetric; sort: AgentsSort }): string {
	return `${view.metric}:${view.sort}`;
}

function agentsSelection(grain: MeasurementGrain, scope: MeasurementScope, view: { metric: AgentsMetric; sort: AgentsSort }): string {
	return `${grain}:${agentsScopeKey(scope)}:${agentsViewKey(view)}`;
}

/** What the project lists answer for: the composite scope and one served Trend grain. */
function agentsSummariesKey(scopeKey: string, grain: MeasurementGrain): string {
	return `${scopeKey}:${grain}`;
}

/** Load the Agents table for one composite scope, one column and one order. */
export async function loadMeasurementAgents(
	grain = get(measurementGrain),
	scope = get(measurementScope),
	view = get(measurementAgentsView),
): Promise<void> {
	controllers.get('agents')?.abort();
	const controller = new AbortController();
	controllers.set('agents', controller);
	const selection = agentsSelection(grain, scope, view);
	const scopeKey = agentsScopeKey(scope);
	const viewKey = agentsViewKey(view);
	measurementAgents.update((state) => {
		// What stays on screen while this request is in flight. The findings (the grain's
		// last periods) and the rows (the grain's latest window) both move with the grain,
		// so on a grain change they stay only until this response replaces them; the
		// findings do not depend on the sorted column, so a column change keeps them. The
		// ranked rows and the order they claim answer to the selected column, so those
		// clear when it changes. A new scope clears everything: nothing is carried across
		// a scope boundary.
		const sameScope = state.scopeKey === scopeKey;
		const sameView = sameScope && state.servedView === viewKey;
		// The project lists are counted for one specific Trend grain, so they survive only
		// a reload that asks for the very grain they were served under. A grain change
		// clears them instead of leaving the previous grain's counts looking current.
		const sameSummaries = state.summariesKey === agentsSummariesKey(scopeKey, grain);
		return {
			...state,
			loading: true,
			error: null,
			selection,
			scopeKey,
			servedView: viewKey,
			rows: sameView ? state.rows : [],
			sort: sameView ? state.sort : null,
			findings: sameScope ? state.findings : [],
			findingsState: sameScope ? state.findingsState : null,
			caption: sameScope ? state.caption : '',
			grain: sameScope ? state.grain : null,
			period: sameScope ? state.period : null,
			projectSummaries: sameSummaries ? state.projectSummaries : [],
			inactiveProjects: sameSummaries ? state.inactiveProjects : [],
			summariesKey: sameSummaries ? state.summariesKey : null,
		};
	});
	try {
		const params = grainScopeParams(grain, scope);
		params.set('metric', view.metric);
		params.set('sort', view.sort);
		const response = await fetch(`/api/measurement/agents?${params}`, { signal: controller.signal });
		if (!response.ok) throw new Error('Could not load agents');
		const body = await response.json();
		if (controllers.get('agents') !== controller
			|| selection !== agentsSelection(get(measurementGrain), get(measurementScope), get(measurementAgentsView))) return;
		// Bytes that answer a selection the reader already left are discarded above, before
		// they are checked: they are not a failure of the surface, they are irrelevant.
		// What is checked here is the answer the reader is still looking at, against the
		// contract this surface consumes, never defaulted — an answer that cannot support
		// an empty state, an order claim or a findings verdict belongs to the failed-load
		// path below, which is the only honest one for it.
		const served = decodeAgentsResponse(body, {
			project: scope.project,
			agent: scope.agent,
			metric: view.metric,
			sort: view.sort,
			grain,
		});
		// The lists are bound to the grain the response echoed, not to the grain the
		// reader happened to ask for, so a key is always written from the bytes served.
		measurementAgents.set({
			loading: false,
			error: null,
			rows: served.rows as AgentsState['rows'],
			findings: served.findings as AgentsState['findings'],
			findingsState: served.findingsState,
			caption: served.caption,
			selection,
			scopeKey,
			servedView: viewKey,
			grain: served.grain as MeasurementGrain,
			period: served.period,
			sort: served.sort as AgentsState['sort'],
			projectSummaries: served.projectSummaries as AgentsState['projectSummaries'],
			inactiveProjects: served.inactiveProjects as AgentsState['inactiveProjects'],
			summariesKey: agentsSummariesKey(scopeKey, served.grain as MeasurementGrain),
		});
	} catch (error) {
		if ((error as DOMException).name === 'AbortError' || controllers.get('agents') !== controller) return;
		// A failed load cannot support an order claim or a row, so both go; the scope's
		// last served findings stay with the retry the panel offers rather than flashing
		// to nothing.
		measurementAgents.update((state) => ({
			...state,
			loading: false,
			error: 'Could not load the agents table.',
			selection,
			rows: [],
			sort: null,
			findings: state.scopeKey === scopeKey ? state.findings : [],
			findingsState: state.scopeKey === scopeKey ? state.findingsState : null,
			caption: state.scopeKey === scopeKey ? state.caption : '',
		}));
	}
}
