/**
 * Operator plan execution wire types -- who runs a node, what holds it, and
 * its attempts, as published by `backend/plans/operator_projection.py`.
 */

export interface OperatorExecutor {
	thread_id: string | null;
	label: string;
}

export interface OperatorExecutionCause {
	kind: 'waiting' | 'blocked' | 'paused';
	reason_key: string;
	reason: string;
	owner_kind: 'user' | 'thread' | 'external' | 'system';
	owner_id: string | null;
	dependency_node_id?: string;
	watch_id?: string;
	/** When a watch's wait ends on its own: its deadline, or null for an event watch. */
	ends_at?: string | null;
	/** Why the agent set this task aside until ends_at: a scheduled task. */
	scheduled_reason?: string;
	id?: string;
	created_at?: string;
}

export interface OperatorSelection {
	attempt_id: string;
	attempt_ordinal: number;
	executor_thread_id: string;
	requirements_revision: number;
	turn_active: boolean;
	in_flight_dispatch_count: number;
}

export interface OperatorAttempt {
	id: string;
	ordinal: number;
	executor: OperatorExecutor;
	requirements_revision: number;
	selected_at: string;
	released_at: string | null;
	release_reason: string | null;
}

export interface OperatorExecution {
	condition: 'ready' | 'running' | 'waiting' | 'blocked' | 'paused' | 'review' | 'terminal';
	primary_reason: string | null;
	causes: OperatorExecutionCause[];
	selection: OperatorSelection | null;
	conflict: {
		condition: string;
		selected_node_id: string | null;
		selected_node_title: string | null;
	} | null;
	attempt_count: number;
	/** Retries a failed or rejected try forced; edits and other releases never count. */
	friction_count: number;
	/** Send-back chain round for a revision; only repeated rounds are shown. */
	revision_round?: number;
	attempts: OperatorAttempt[];
}
