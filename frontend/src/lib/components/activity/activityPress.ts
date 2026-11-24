import type { ActivityTaskDetail } from '$lib/api/activityQueue';
import type { AnswerOutcome } from '$lib/api/attentionQueue';
import { decideHeldReply, type HeldOutcome, type HeldVerb } from '$lib/api/heldReplies';
import { mutateUserTask, type PlanLifecycleMutationOutcome } from '$lib/api/plans';
import { clearExpiredSession } from '$lib/stores/auth';

/** The presses in Activity's band; a failed one's button reads Try again. */
export type PressAction = 'answer' | 'send' | 'dont_send' | 'done';

/**
 * What a press came to, in Activity's terms. `refused`: the server answered and pressing again can't change it, so it
 * shows without Try again (`final` when the item has also left the server's list). `failed`: it didn't go through,
 * and Try again may; `reference` is the server's id for a system failure, shown beside the line as the dock shows it.
 */
export type Pressed =
	| { outcome: 'succeeded' }
	| { outcome: 'refused'; reason: string; text: string | null; final?: boolean }
	| { outcome: 'failed'; text: string; reference?: string };

export const SUCCEEDED: Pressed = { outcome: 'succeeded' };

/** Send or Don't send, through the call the dock uses; an expired session signs out, as it does there. */
export async function decideHeldPress(id: string, verb: HeldVerb): Promise<Pressed> {
	const outcome = await decideHeldReply(id, verb);
	if (outcome.status === 'unauthorized') clearExpiredSession();
	return heldPressed(outcome, verb === 'approve');
}

/** Done on a task, as the person's step Done in a plan settles it. */
export async function markTaskDonePress(task: ActivityTaskDetail): Promise<Pressed> {
	const key = `mark_done:${task.node_id}:${task.revision}`;
	return taskPressed(await mutateUserTask(task.plan_id, 'mark_done', task.node_id, task.revision, key));
}

/** A decision's answer. */
export function answerPressed(outcome: AnswerOutcome): Pressed {
	switch (outcome.status) {
		case 'ok': return { outcome: 'succeeded' };
		// Answered or closed elsewhere first: the server's sentence says which.
		case 'gone': return { outcome: 'refused', reason: 'gone', text: outcome.message, final: true };
		// The re-read shows the decision's latest revision, to answer again.
		case 'stale': return { outcome: 'refused', reason: 'changed', text: 'This decision changed. Showing the latest.' };
		case 'rejected': return { outcome: 'refused', reason: 'rejected', text: outcome.message };
		case 'unauthorized': return { outcome: 'refused', reason: 'unavailable', text: 'This decision isn’t available.' };
		default: return { outcome: 'failed', text: 'Couldn’t save your answer. Try again.' };
	}
}

/** Send or Don't send on a held message. */
export function heldPressed(outcome: HeldOutcome, approve: boolean): Pressed {
	switch (outcome.status) {
		// Settled: another page decided it first; it simply leaves the list.
		case 'sent': case 'declined': case 'settled': return { outcome: 'succeeded' };
		// Approved, but it couldn't be delivered (its recipient is gone, say); the server's line says why.
		case 'not_sent': return {
			outcome: 'refused', reason: outcome.reason ?? 'not_sent', text: outcome.line || 'Couldn’t send this message.', final: true,
		};
		// The session ended; signing in again takes over the screen.
		case 'unauthorized': return { outcome: 'refused', reason: 'signed_out', text: null };
		default: return {
			outcome: 'failed',
			text: outcome.line ?? (approve ? 'Couldn’t send this message. Try again.' : 'Couldn’t decline this message. Try again.'),
			...(outcome.reference ? { reference: outcome.reference } : {}),
		};
	}
}

/** Done on a task only the person can do. */
export function taskPressed(outcome: PlanLifecycleMutationOutcome): Pressed {
	switch (outcome.status) {
		case 'success': return { outcome: 'succeeded' };
		case 'conflict': return { outcome: 'refused', reason: 'changed', text: 'This task changed. Showing the latest.' };
		case 'forbidden': return {
			outcome: 'refused', reason: 'forbidden', text: 'You can’t change this task. Ask the agent in chat to change it.',
		};
		// The server's own reason for refusing: pressing again won't change it.
		default: return outcome.refused
			? { outcome: 'refused', reason: 'rejected', text: outcome.message }
			: { outcome: 'failed', text: 'Couldn’t mark this task done. Try again.' };
	}
}
