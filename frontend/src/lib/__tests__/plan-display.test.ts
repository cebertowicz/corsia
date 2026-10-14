import { describe, expect, it } from 'vitest';
import {
	cardStateLabel,
	cardStateTone,
	nodeDisplayLabel,
	nodeDisplayState,
	nodeStateTone,
	planCardState,
} from '$lib/components/plans/planDisplay';
import type {
	OperatorNode,
	OperatorPlanEntry,
	OperatorPlanLabelKey,
} from '$lib/api/plans';

const DISTRIBUTION = {
	done: 0, active: 0, pending_ready: 1, review_parent_agent: 0,
	review_human: 0, waiting: 0, blocked: 0, paused: 0, failed: 0,
};

const COST = {
	commitment_id: 'n1',
	attempts: { total: 0, failed: 0, correction: 0, delegated: 0 },
	tokens: { known: null, unknown_rows: 0 },
	model_cost_usd: { known: null, unknown_rows: 0 },
	usage_recording_failures: 0,
	elapsed_seconds: { known: 0, status: 'in_progress' as const },
	user_effort: { known_plan_actions: 0, status: 'partial' as const },
};

const AMOUNT = {
	tokens: { known: null, unknown_rows: 0 },
	model_cost_usd: { known: null, unknown_rows: 0 },
	usage_recording_failures: 0,
	execution: { seconds: 0, unknown_turns: 0, turns: { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 } },
	waiting: { seconds: 0 },
};
const NODE_COST = { own: AMOUNT, rolled: AMOUNT };

function entry(over: Partial<OperatorPlanEntry> = {}): OperatorPlanEntry {
	return {
		plan: {
			id: 'p1', owner_kind: 'thread', owner_id: 't1',
			state: 'active', revision: 1, updated_at: 'now',
		},
		owner: { label: 'Thread', project_id: null, thread_id: 't1' },
		progress: { done: 0, total: 1, open: 1, liveness: 'idle', distribution: DISTRIBUTION },
		display: { label_key: 'waiting', tone_key: 'waiting' },
		oversize: null,
		current_node_ids: [],
		next_node_ids: [],
		nodes: [], folds: [], root_count: 0,
		...over,
	};
}

function node(over: Partial<OperatorNode> = {}): OperatorNode {
	return {
		id: 'n1', parent_id: null, depth: 0, kind: 'task', title: 'Task',
		description: null, acceptance_criteria: 'DOD', state: 'pending',
		unmet_dependencies: [], held_by: null, revision: 1, requirements_revision: 1,
		commitment_id: 'n1', updated_at: 'now', last_transition_at: 'now', folded: false,
		requires_user_action: false,
		executor: { thread_id: 't1', label: 'Agent' },
		execution: {
			condition: 'ready', primary_reason: null, causes: [], selection: null,
			conflict: null, attempt_count: 0, friction_count: 0, attempts: [],
		},
		display: { label_key: 'working', tone_key: 'active' },
		status_reason: null, evidence: null, commitment_cost: COST, node_cost: NODE_COST, initiative: null, branch_progress: null, attention_path: [],
		completion: null, child: null, review: null,
		discussion_artifact_id: null, artifacts: [], user_actions: [],
		...over,
	};
}

describe('contract-v2 server display keys', () => {
	it('uses the server plan key despite contradictory lifecycle and liveness fields', () => {
		const value = entry({
			plan: {
				id: 'p1', owner_kind: 'thread', owner_id: 't1',
				state: 'paused', revision: 1, updated_at: 'now',
			},
			progress: {
				done: 1, total: 1, open: 0, liveness: 'advancing',
				distribution: { ...DISTRIBUTION, done: 1, pending_ready: 0 },
			},
			display: { label_key: 'working', tone_key: 'active' },
		});
		expect(planCardState(value)).toBe('working');
		expect(cardStateTone(value)).toBe('active');
	});

	it('fails visibly when a future server display key is unknown', () => {
		const futurePlan = entry();
		futurePlan.display = {
			label_key: 'future_plan_state' as OperatorPlanEntry['display']['label_key'],
			tone_key: 'future_tone' as OperatorPlanEntry['display']['tone_key'],
		};
		const futureNode = node();
		futureNode.display = {
			label_key: 'future_node_state' as OperatorNode['display']['label_key'],
			tone_key: 'future_tone' as OperatorNode['display']['tone_key'],
		};

		expect(planCardState(futurePlan)).toBe('unknown');
		expect(cardStateLabel('unknown')).toBe('Unavailable');
		expect(cardStateTone(futurePlan)).toBe('unknown');
		expect(nodeDisplayState(futureNode)).toBe('unknown');
		expect(nodeDisplayLabel(futureNode)).toBe('Unavailable');
		expect(nodeStateTone(futureNode)).toBe('unknown');
	});

	it('uses the server node keys despite contradictory lifecycle and blocked fields', () => {
		const value = node({
			state: 'accepted',
			unmet_dependencies: [], held_by: 'user',
			display: { label_key: 'waiting', tone_key: 'waiting' },
		});
		expect(nodeDisplayState(value)).toBe('waiting');
		expect(nodeDisplayLabel(value, true)).toBe('Waiting');
		expect(nodeStateTone(value)).toBe('waiting');
	});

	it('renders every server node label key', () => {
		const labels = {
			working: 'Working',
			needs_you: 'Needs you',
			waiting: 'Waiting',
			finished: 'Finished',
		} as const;
		for (const [label_key, label] of Object.entries(labels)) {
			expect(nodeDisplayLabel(node({
				display: { label_key: label_key as keyof typeof labels, tone_key: 'waiting' },
			}))).toBe(label);
		}
	});

	it('renders a closed node in its own quiet tone, never as complete or failed', () => {
		const value = node({
			state: 'closed',
			display: { label_key: 'finished', tone_key: 'closed' },
		});
		expect(nodeDisplayLabel(value)).toBe('Finished');
		expect(nodeStateTone(value)).toBe('closed');
		expect(nodeDisplayState(value)).toBe('finished');
	});

	it('labels every server plan state', () => {
		// Exhaustive by construction: a new OperatorPlanLabelKey that nobody
		// maps fails here rather than silently rendering "Unavailable" to a
		// user. The Record type makes omission a compile error too.
		const expected: Record<OperatorPlanLabelKey, string> = {
			working: 'Working',
			needs_you: 'Needs you',
			waiting: 'Waiting',
			finished: 'Finished',
		};
		for (const [key, label] of Object.entries(expected)) {
			const value = entry();
			value.display = {
				label_key: key as OperatorPlanLabelKey,
				tone_key: 'waiting',
			};
			const state = planCardState(value);
			expect(state, `${key} must map to a known card state`).not.toBe('unknown');
			expect(cardStateLabel(state)).toBe(label);
		}
	});
});
