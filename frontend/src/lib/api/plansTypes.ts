/**
 * Operator plan wire types — the exact contract published by
 * `backend/plans/operator_projection.py`.
 */

import type { OperatorCommitmentCost, OperatorInitiative, OperatorNodeCost } from './plansCostTypes';
import type { OperatorExecution, OperatorExecutor } from './plansExecutionTypes';
import type { OperatorFoldLevel } from './plansFolded';

export type OperatorLiveness =
	| 'advancing'
	| 'idle'
	| 'blocked'
	| 'needs-review'
	| 'waiting'
	| 'paused';
export type OperatorOwnerKind = 'workspace_scope' | 'project_scope' | 'thread';
export type OperatorToneKey =
	| 'active'
	| 'human-review'
	| 'waiting'
	| 'complete'
	| 'failed'
	| 'closed';
export type OperatorNodeLabelKey =
	| 'working'
	| 'needs_you'
	| 'needs_agent_input'
	| 'waiting'
	| 'finished';
export type OperatorPlanLabelKey =
	| 'working'
	| 'needs_you'
	| 'waiting'
	| 'finished';

/** Agent-continuation facts for a still plan; human surfaces keep the four-state label. */
export type OperatorNotMoving = {
	need_you: number;
	next_wait_ends_at: string | null;
	next_overdue_at: string | null;
	overdue_waits: number;
	overdue_wait_entries: { title: string; node_id: string }[];
	paused_by_you: number;
	set_aside?: number;
};

export interface OperatorDisplay<Label extends string = string> {
	label_key: Label;
	tone_key: OperatorToneKey;
}

export interface OperatorPlanMeta {
	id: string;
	owner_kind: OperatorOwnerKind;
	owner_id: string;
	state: string;
	revision: number;
	updated_at: string;
}

export interface OperatorOwner {
	label: string;
	project_id: string | null;
	thread_id: string | null;
}

export interface OperatorDistribution {
	done: number;
	active: number;
	pending_ready: number;
	review_parent_agent: number;
	review_human: number;
	waiting: number;
	blocked: number;
	paused: number;
	failed: number;
}

export interface OperatorProgress {
	done: number;
	total: number;
	/** Unsettled leaves in play, shown as "N left": done/total would read 0 of N in a flat plan. */
	open: number;
	liveness: OperatorLiveness;
	distribution?: OperatorDistribution;
}

/** What a plan waits on outside itself and when its agent checks back (EW3). */
export interface OperatorWait {
	label: string;
	/** When the watch's deadline passes; the agent is woken then. */
	until: string;
	/** How many other waits there are. */
	more: number;
	/** The watched artifact, when the wait watches one. */
	artifact_id?: string | null;
}

export interface OperatorChildSummary {
	available: true;
	plan_id: string;
	thread_id: string;
	progress: OperatorProgress;
	/** What a waiting child plan waits for (EW4). Its state stays `progress.liveness`. */
	wait?: OperatorWait | null;
}

export interface OperatorReviewActor {
	kind: 'user' | 'thread' | 'external';
	id: string | null;
	label: string;
}

export interface OperatorReview {
	kind: 'human_decision' | 'human_review' | 'parent_agent' | 'external';
	label: string;
	actor: OperatorReviewActor;
}

export interface OperatorCompletion {
	event_id: string;
	at: string;
	witnessed: boolean;
}

export interface OperatorArtifactLink {
	id: string;
	role: string;
	integration: string;
	type: string;
	external_id: string;
	title: string | null;
	path: string | null;
	state: string;
	attention_state: string | null;
}

export interface OperatorEvidence {
	evidence: string;
	artifact_ids: string[];
	requirements_revision: number;
}

/** A control the user has on an open task; the server decides which apply. */
export type OperatorUserAction = 'mark_done' | 'pause_node' | 'resume_node' | 'close_node';

/**
 * Applies one of those controls to a task. A handler that reports back returns
 * the server's own refusal text, or null once the action landed; the control
 * the user clicked shows that refusal beside itself.
 */
export type UserActionHandler = (
	node: OperatorNode,
	action: OperatorUserAction,
	reason?: string,
) => Promise<string | null> | void;

export interface OperatorNode {
	/** What this row waits on outside the plan and when its agent checks back (EW11); the thread state's value. */
	wait?: OperatorWait | null;
	id: string;
	parent_id: string | null;
	depth: number;
	kind: string;
	title: string;
	description: string | null;
	/** Open steps in this plan that this user-assigned step releases. */
	waiting_on: string[];
	acceptance_criteria: string;
	state: string;
	/** Prerequisites not yet accepted; empty unless the node is open. */
	unmet_dependencies: string[];
	/** Owner kind of the block placed on the node, if any. */
	held_by: string | null;
	/** Who the task is assigned to: "user", "coordinator", "subagent", or null. */
	assignee: string | null;
	revision: number;
	requirements_revision: number;
	commitment_id: string;
	updated_at: string;
	last_transition_at: string;
	/**
	 * Finished and settled over a day ago (`plans/node_states.py`): the tree
	 * gathers it into its level's "N completed" row. Still counted as done.
	 */
	folded: boolean;
	/** A branch that reads as its leaves (`node_states.is_container`) and has no ask of its own; its need is theirs, counted once. */
	container: boolean;
	requires_user_action: boolean;
	executor: OperatorExecutor;
	execution: OperatorExecution;
	display: OperatorDisplay<OperatorNodeLabelKey>;
	/** The one specific cause a row names under its title ("After Build"), if any. */
	status_reason: string | null;
	evidence: OperatorEvidence | null;
	/** Present only on the commitment's one root node; null everywhere else. */
	commitment_cost: OperatorCommitmentCost | null;
	node_cost: OperatorNodeCost;
	/** Lineage total, on every task of a lineage of two or more; null elsewhere. */
	initiative: OperatorInitiative | null;
	branch_progress: { done: number; total: number } | null;
	/** When the work under this node, all of it quietly set aside by its agent, next comes due (ISO); null if any open work is not. */
	scheduled_until: string | null;
	attention_path: string[];
	completion: OperatorCompletion | null;
	child: OperatorChildSummary | null;
	review: OperatorReview | null;
	user_actions: OperatorUserAction[];
	discussion_artifact_id: string | null;
	artifacts: OperatorArtifactLink[];
}

export interface OperatorPlanEntry {
	plan: OperatorPlanMeta;
	owner: OperatorOwner;
	progress: OperatorProgress & { distribution: OperatorDistribution };
	display: OperatorDisplay<OperatorPlanLabelKey> & { not_moving?: OperatorNotMoving; wait?: OperatorWait; set_aside_until?: string };
	/** Present only when open nodes exceed the server's soft budget. */
	oversize: { open: number; budget: number } | null;
	current_node_ids: string[];
	next_node_ids: string[];
	/** Open rows, plus the window of folded rows this read lists (`folds` says what the rest of each level is). */
	nodes: OperatorNode[];
	folds: OperatorFoldLevel[];
	/** Every top-level root of the plan, folded ones included: the sole-wrapper rule reads it, not `nodes`. */
	root_count: number;
}

export interface OperatorTarget {
	kind: OperatorOwnerKind;
	id: string;
	label: string;
}

export interface OperatorAttention {
	awaiting_count: number;
}

export interface OperatorEnvelope {
	contract_version: 2;
	as_of: string;
	target: OperatorTarget;
	revision_token: string;
	plans: OperatorPlanEntry[];
	attention: OperatorAttention;
	/**
	 * Thread reads only: what the reader has seen of this plan in the pane
	 * (`plans.pane_views`). Every node settled at or before `seen_through` has
	 * been on screen; null before the reader's first pane view.
	 */
	seen_through?: string | null;
}

export type OperatorReadOutcome =
	| { status: 'fresh'; payload: OperatorEnvelope; etag: string }
	| { status: 'not-modified' }
	| { status: 'not-found' }
	| { status: 'unauthorized' }
	| { status: 'forbidden' }
	| { status: 'error'; message: string };
