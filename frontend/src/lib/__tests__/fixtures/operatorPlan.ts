import type {
	OperatorCostAmount,
	OperatorDistribution,
	OperatorNode,
	OperatorPlanEntry,
	OperatorPlanLabelKey,
	OperatorToneKey,
} from '$lib/api/plans';

const EMPTY_AMOUNT: OperatorCostAmount = {
	tokens: { known: null, unknown_rows: 0 },
	model_cost_usd: { known: null, unknown_rows: 0 },
	usage_recording_failures: 0,
	execution: { seconds: 0, unknown_turns: 0, turns: { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 } },
	waiting: { seconds: 0 },
};

export const EMPTY_DISTRIBUTION: OperatorDistribution = {
	done: 0,
	active: 0,
	pending_ready: 0,
	review_parent_agent: 0,
	review_human: 0,
	waiting: 0,
	blocked: 0,
	paused: 0,
	failed: 0,
};

export function planDisplay(
	label_key: OperatorPlanLabelKey = 'waiting',
	tone_key: OperatorToneKey = 'waiting',
) {
	return { label_key, tone_key };
}

export function operatorNode(over: Partial<OperatorNode> = {}): OperatorNode {
	const id = over.id ?? 'node';
	const state = over.state ?? 'pending';
	const review = over.review ?? null;
	// Only a hold the user must clear reads red (#2213).
	const blocked = over.held_by === 'user';
	const condition = over.execution?.condition
		?? (state === 'active'
			? 'running'
			: state === 'review_requested'
				? 'review'
				: state === 'accepted' || state === 'accepted_with_followup' || state === 'failed'
					? 'terminal'
					: blocked
						? 'blocked'
						: 'ready');
	const display = over.display ?? (
		state === 'accepted' || state === 'accepted_with_followup'
			? { label_key: 'finished', tone_key: 'complete' }
			: state === 'failed'
				? { label_key: 'finished', tone_key: 'failed' }
				: condition === 'review'
					? review?.kind === 'human_decision' || review?.kind === 'human_review'
						? { label_key: 'needs_you', tone_key: 'human-review' }
						: review?.kind === 'external'
							? { label_key: 'waiting', tone_key: 'waiting' }
							: { label_key: 'working', tone_key: 'active' }
					: condition === 'running' || condition === 'ready'
						? { label_key: 'working', tone_key: 'active' }
						: { label_key: 'waiting', tone_key: 'waiting' }
	);

	return {
		id,
		parent_id: null,
		depth: 0,
		kind: 'task',
		title: 'Task',
		description: null,
		acceptance_criteria: 'DOD',
		state,
		unmet_dependencies: [],
		held_by: null,
		assignee: null,
		revision: 1,
		requirements_revision: 1,
		commitment_id: id,
		updated_at: 'now',
		last_transition_at: over.updated_at ?? 'now',
		folded: false,
		requires_user_action: review?.kind === 'human_decision' || review?.kind === 'human_review',
		executor: { thread_id: 'agent', label: 'Agent' },
		execution: {
			condition,
			primary_reason: null,
			causes: [],
			selection: null,
			conflict: null,
			attempt_count: 0,
			friction_count: 0,
				attempts: [],
		},
		display,
		evidence: null,
		commitment_cost: null,
		node_cost: { own: EMPTY_AMOUNT, rolled: EMPTY_AMOUNT },
		initiative: null,
		status_reason: null,
		branch_progress: null,
		scheduled_until: null,
		attention_path: [],
		completion: null,
		child: null,
		review,
		discussion_artifact_id: null,
		user_actions: [],
		artifacts: [],
		...over,
	};
}

export function withPlanContract(
	entry: Omit<OperatorPlanEntry, 'display' | 'progress' | 'oversize'> & {
		progress: Omit<OperatorPlanEntry['progress'], 'distribution'>;
		display?: OperatorPlanEntry['display'];
		oversize?: OperatorPlanEntry['oversize'];
	},
): OperatorPlanEntry {
	return {
		...entry,
		progress: { ...entry.progress, distribution: { ...EMPTY_DISTRIBUTION } },
		display: entry.display ?? planDisplay(),
		oversize: entry.oversize ?? null,
	};
}
