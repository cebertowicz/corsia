import type { OperatorExecution, OperatorNode } from '$lib/api/plans';
import { operatorNode } from './operatorPlan';

/**
 * One plan shaped like the reported disclosure defect, shared by every surface
 * that renders `PlanTree`: a blocked path, an unrelated phase holding the Next
 * task, a paused phase, and completed work.
 */
export interface PlanTreeShape {
	nodes: OperatorNode[];
	currentNodeIds: string[];
	nextNodeIds: string[];
}

const BLOCKED_CAUSE = {
	kind: 'blocked' as const,
	reason_key: 'partner_api',
	reason: 'Waiting for the partner API',
	owner_kind: 'external' as const,
	owner_id: null,
};

const PAUSE_HOLD = {
	kind: 'paused' as const,
	reason_key: 'manual_pause',
	reason: 'Held for the release window',
	owner_kind: 'thread' as const,
	owner_id: 'agent',
};

const PLAN_PAUSE = {
	kind: 'paused' as const,
	reason_key: 'plan_paused',
	reason: 'Plan is paused',
	owner_kind: 'thread' as const,
	owner_id: 'owner',
};

function execution(condition: OperatorExecution['condition'], causes: OperatorExecution['causes']): OperatorExecution {
	return {
		condition,
		primary_reason: causes[0]?.reason ?? null,
		causes,
		selection: null,
		conflict: null,
		attempt_count: 0,
		friction_count: 0,
		attempts: [],
	};
}

/** An active plan with a neutral external wait: only Build holds that path. */
export function blockedPlanTree(): PlanTreeShape {
	const nodes = [
		operatorNode({ id: 'build', title: 'Build', kind: 'phase', branch_progress: { done: 1, total: 2 } }),
		operatorNode({ id: 'kickoff', title: 'Kickoff', parent_id: 'build', depth: 1, state: 'accepted' }),
		operatorNode({
			id: 'wait-api', title: 'Wait for API', parent_id: 'build', depth: 1, state: 'active',
			unmet_dependencies: [], held_by: 'user', execution: execution('blocked', [BLOCKED_CAUSE]),
			display: { label_key: 'waiting', tone_key: 'waiting' }, attention_path: ['build', 'wait-api'],
		}),
		operatorNode({ id: 'docs', title: 'Docs', kind: 'phase', branch_progress: { done: 0, total: 1 } }),
		operatorNode({
			id: 'write-guide', title: 'Write guide', parent_id: 'docs', depth: 1,
			display: { label_key: 'working', tone_key: 'active' },
		}),
		operatorNode({ id: 'polish', title: 'Polish', kind: 'phase', branch_progress: { done: 0, total: 1 } }),
		operatorNode({
			id: 'tune', title: 'Tune', parent_id: 'polish', depth: 1, state: 'active',
			execution: execution('paused', [PAUSE_HOLD]), display: { label_key: 'waiting', tone_key: 'waiting' },
		}),
	];
	return { nodes, currentNodeIds: ['wait-api'], nextNodeIds: ['write-guide'] };
}

/**
 * The same plan after a whole-plan pause, as the server projects it: every
 * nonterminal node reads Waiting, the blocker survives only as a detail cause, and
 * nothing is current, next, or on an attention path.
 */
export function pausedPlanTree(): PlanTreeShape {
	const nodes = blockedPlanTree().nodes.map((node) => {
		if (node.execution.condition === 'terminal') return node;
		return {
			...node,
			execution: execution('paused', [PLAN_PAUSE, ...node.execution.causes]),
			display: { label_key: 'waiting' as const, tone_key: 'waiting' as const },
			attention_path: [],
		};
	});
	return { nodes, currentNodeIds: [], nextNodeIds: [] };
}
