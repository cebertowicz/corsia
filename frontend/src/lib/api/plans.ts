/**
 * Operator plan API client — typed reads over the P4 rollup contract
 * (`backend/plans/operator_projection.py`) plus the existing generic
 * mutation action. ETag-aware; ETag/GET stays the sole truth source. A 2xx
 * body is admitted only once its decoder proves it is the envelope
 * (`plansDecode`); anything else is a failed read, reported as one.
 */
import type { ActivityIndexReadOutcome } from './activityIndexTypes';
import { decodeActivityIndexEnvelope } from './activityIndexDecode';
import type { OperatorReadOutcome, OperatorUserAction } from './plansTypes';
import { decodeOperatorEnvelope } from './plansDecode';

export * from './activityIndexTypes';
export * from './plansCostTypes';
export * from './plansTypes';
export type { OperatorFoldLevel } from './plansFolded';


import { authHeaders, readPlan, safeErrorMessage } from './planRead';

async function readOperator(
	url: string,
	bearerValue: string,
	etag: string | null | undefined,
): Promise<OperatorReadOutcome> {
	return readPlan(url, bearerValue, etag, decodeOperatorEnvelope);
}

async function readActivityIndex(
	url: string,
	bearerValue: string,
	etag: string | null | undefined,
): Promise<ActivityIndexReadOutcome> {
	return readPlan(url, bearerValue, etag, decodeActivityIndexEnvelope);
}


export type PlanLifecycleOperation = 'pause' | 'resume';
export type PlanLifecycleMutationOutcome =
	| { status: 'success' }
	| { status: 'conflict'; detail?: string }
	| { status: 'forbidden' }
	| { status: 'error'; message: string };

/** Post one audited, revision-safe plan mutation as the authenticated user. */
async function postPlanMutation(
	bearerValue: string,
	planId: string,
	body: { operation: string; mutation_id: string; params: Record<string, unknown> },
	failure: string,
	showRefusal = false,
): Promise<PlanLifecycleMutationOutcome> {
	let res: Response;
	try {
		res = await fetch(`/api/plans/${encodeURIComponent(planId)}/mutations`, {
			method: 'POST',
			headers: {
				Authorization: `Bearer ${bearerValue}`,
				'Content-Type': 'application/json',
			},
			body: JSON.stringify(body),
		});
	} catch {
		return { status: 'error', message: failure };
	}
	if (res.ok) return { status: 'success' };
	if (res.status === 403) return { status: 'forbidden' };
	// A mutation the server refuses names its own reason (409 moved, 422 refused),
	// written for the person who sent it; a generic line would only hide it.
	const detail = showRefusal ? await refusalDetail(res) : undefined;
	if (res.status === 409) return { status: 'conflict', detail };
	if (res.status === 422 && detail) return { status: 'error', message: detail };
	return { status: 'error', message: safeErrorMessage(res.status) };
}

async function refusalDetail(res: Response): Promise<string | undefined> {
	try {
		const body: unknown = await res.json();
		const detail = (body as { detail?: unknown } | null)?.detail;
		return typeof detail === 'string' && detail.trim() ? detail : undefined;
	} catch {
		return undefined;
	}
}

/** Apply one closed, revision-safe whole-plan lifecycle mutation. */
export async function mutatePlanLifecycle(
	bearerValue: string,
	planId: string,
	operation: PlanLifecycleOperation,
	expectedRevision: number,
	mutationId: string,
): Promise<PlanLifecycleMutationOutcome> {
	return postPlanMutation(bearerValue, planId, {
		operation,
		mutation_id: mutationId,
		params: { expected_revision: expectedRevision },
	}, 'Could not update this plan.');
}

/** Apply the user's control on a task: Mark done, Pause, Resume or Won't do (with its reason). */
export async function mutateUserTask(
	bearerValue: string,
	planId: string,
	operation: OperatorUserAction,
	nodeId: string,
	expectedRevision: number,
	mutationId: string,
	reason?: string,
): Promise<PlanLifecycleMutationOutcome> {
	return postPlanMutation(bearerValue, planId, {
		operation,
		mutation_id: mutationId,
		params: {
			node_id: nodeId,
			expected_revision: expectedRevision,
			...(reason ? { reason } : {}),
		},
	}, 'Could not update this task.', true);
}


export async function readActivityWorkspace(
	bearerValue: string, etag?: string | null,
): Promise<ActivityIndexReadOutcome> {
	return readActivityIndex('/api/plans/activity/scopes/workspace', bearerValue, etag);
}

export async function readActivityProject(
	bearerValue: string, projectId: string, etag?: string | null,
): Promise<ActivityIndexReadOutcome> {
	return readActivityIndex(
		`/api/plans/activity/scopes/projects/${encodeURIComponent(projectId)}`,
		bearerValue,
		etag,
	);
}

export async function readOperatorWorkspace(
	bearerValue: string, etag?: string | null,
): Promise<OperatorReadOutcome> {
	return readOperator('/api/plans/operator/scopes/workspace', bearerValue, etag);
}

export async function readOperatorProject(
	bearerValue: string, projectId: string, etag?: string | null,
): Promise<OperatorReadOutcome> {
	return readOperator(
		`/api/plans/operator/scopes/projects/${encodeURIComponent(projectId)}`,
		bearerValue,
		etag,
	);
}

export async function readOperatorThread(
	bearerValue: string, threadId: string, etag?: string | null,
): Promise<OperatorReadOutcome> {
	return readOperator(`/api/plans/operator/threads/${encodeURIComponent(threadId)}`, bearerValue, etag);
}

/**
 * Report that the pane showed this thread's plan: everything settled by
 * `seenThrough` was on screen. The server only moves it forward. False when
 * the report did not land.
 */
export async function recordThreadPlanSeen(bearerValue: string, threadId: string, seenThrough: string): Promise<boolean> {
	try {
		const res = await fetch(`/api/plans/operator/threads/${encodeURIComponent(threadId)}/seen`, {
			method: 'POST',
			headers: { Authorization: `Bearer ${bearerValue}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({ seen_through: seenThrough }),
		});
		return res.ok;
	} catch {
		return false;
	}
}

export async function readOperatorChild(
	bearerValue: string, nodeId: string, etag?: string | null,
): Promise<OperatorReadOutcome> {
	return readOperator(`/api/plans/operator/nodes/${encodeURIComponent(nodeId)}/child`, bearerValue, etag);
}

/** The chat list's word for a thread (#2220); the server derives it from canonical plan and lease state. */
export type ThreadPlanState = 'moving' | 'needs_input' | 'blocked_on_you' | 'failed' | 'paused' | 'idle';
export type ThreadAttentionKind = 'decision' | 'review' | 'access' | 'hold' | 'failed';
export interface ThreadAttentionItem {
	kind: ThreadAttentionKind;
	title: string;
	node_id: string | null;
}
export interface ThreadLatestUpdate {
	kind: 'marked' | 'done' | 'done_follow_up' | 'failed' | 'closed' | 'blocked';
	summary: string | null;
}

export interface ThreadPlanStateEntry {
	state: ThreadPlanState;
	/** The plan header's own label for the marker's title; null when the plan row alone decided. */
	label_key: string | null;
	/** The plan is paused, even when what the user owes outranks it (pause contract rule 5). */
	paused: boolean;
	/** Every item represented by the row's single highest-precedence mark. */
	attention_items: ThreadAttentionItem[];
	/** The canonical newest update's bounded sidebar copy; null when its marked message is unavailable. */
	latest_update?: ThreadLatestUpdate | null;
	/** An update arrived since the user's last visit, or they marked it unread; the server's seen state. */
	has_update?: boolean;
	/** The user has opened the thread, so Mark read/unread has a record to write. */
	opened?: boolean;
	/** Plan fact: finished work, nothing open, not paused (AS16). The row also checks activity and higher marks. */
	settled?: boolean;
}

/** Mark a thread read (seen through now) or unread on every device. False when the server refused it. */
export async function markThreadSeen(bearerValue: string, threadId: string, unread: boolean): Promise<boolean> {
	try {
		const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/seen`, {
			method: 'POST',
			headers: { Authorization: `Bearer ${bearerValue}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({ unread }),
		});
		return res.ok;
	} catch {
		return false;
	}
}

/** One batched read of each named thread's state; an absent thread is idle. Null when the read failed. */
export async function readThreadPlanStates(
	bearerValue: string,
	threadIds: string[],
): Promise<Record<string, ThreadPlanStateEntry> | null> {
	const ids = threadIds.map(encodeURIComponent).join(',');
	try {
		const res = await fetch(`/api/plans/operator/thread-states?ids=${ids}`, authHeaders(bearerValue, null));
		if (!res.ok) return null;
		const body = (await res.json()) as { states?: unknown } | null;
		const states = body?.states;
		return states && typeof states === 'object' ? (states as Record<string, ThreadPlanStateEntry>) : null;
	} catch {
		return null;
	}
}
