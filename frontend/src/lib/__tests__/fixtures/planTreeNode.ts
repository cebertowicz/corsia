import type { OperatorCostAmount, OperatorNode } from '$lib/api/plans';

const EMPTY_AMOUNT: OperatorCostAmount = {
	tokens: { known: null, unknown_rows: 0 },
	model_cost_usd: { known: null, unknown_rows: 0 },
	usage_recording_failures: 0,
	execution: { seconds: 0, unknown_turns: 0, turns: { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 } },
	waiting: { seconds: 0 },
};

/**
 * The PlanTree row factory: one operator node with server-owned display keys
 * derived from its lifecycle state, so a test states only what it varies.
 * Shared by every PlanTree suite so the contract has exactly one shape.
 */
export function node(over: Partial<OperatorNode>): OperatorNode {
	const state = over.state ?? 'pending';
	const review = over.review ?? null;
	const userHeld = over.held_by === 'user';
	const condition = over.execution?.condition ?? (
		state === 'active' ? 'running' : state === 'review_requested' ? 'review'
			: state === 'accepted' || state === 'accepted_with_followup' || state === 'failed'
				? 'terminal' : userHeld ? 'blocked' : 'ready'
	);
	const display = userHeld
		? { label_key: 'needs_you' as const, tone_key: 'human-review' as const }
		: state === 'review_requested'
			? review?.kind === 'human_decision' || review?.kind === 'human_review'
				? { label_key: 'needs_you' as const, tone_key: 'human-review' as const }
				: review?.kind === 'external'
					? { label_key: 'waiting' as const, tone_key: 'waiting' as const }
					: { label_key: 'working' as const, tone_key: 'active' as const }
			: state === 'failed'
				? { label_key: 'finished' as const, tone_key: 'failed' as const }
				: state === 'accepted' || state === 'accepted_with_followup'
					? { label_key: 'finished' as const, tone_key: 'complete' as const }
					: condition === 'waiting' || condition === 'paused' || condition === 'blocked' || condition === 'ready'
						? { label_key: 'waiting' as const, tone_key: 'waiting' as const }
						: { label_key: 'working' as const, tone_key: 'active' as const };
	const id = over.id ?? 'n';
	return {
		id, parent_id: null, depth: 0, kind: 'task', title: 'Task',
		description: null, waiting_on: [], acceptance_criteria: 'DOD', state, unmet_dependencies: [], held_by: null, assignee: null,
		revision: 1, requirements_revision: 1, commitment_id: id,
		updated_at: 'now', last_transition_at: 'now', folded: false, container: false, requires_user_action: false,
		executor: { thread_id: 'agent', label: 'Agent' },
		execution: {
			condition,
			primary_reason: null, causes: [], selection: null, conflict: null,
			attempt_count: 0, friction_count: 0, attempts: [],
		},
		display, evidence: null,
		commitment_cost: null,
		node_cost: { own: EMPTY_AMOUNT, rolled: EMPTY_AMOUNT },
		initiative: null,
		status_reason: null,
		branch_progress: null,
		scheduled_until: null,
		attention_path: [], completion: null, child: null, review,
		discussion_artifact_id: null, artifacts: [], user_actions: [], ...over,
	};
}

export const CHILD_AVAILABLE = { available: true as const, plan_id: 'cp1', thread_id: 'child-thread', progress: { done: 1, total: 2, open: 1, liveness: 'advancing' as const } };
