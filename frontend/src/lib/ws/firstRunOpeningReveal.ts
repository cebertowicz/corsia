import type { Message } from '../stores/threads';
import { compareMessageOrder } from '../messageHistory';

const awaitingOpenings = new Set<string>();

/** Mark the local first-model action; history and other devices never set this. */
export function awaitFirstRunOpening(threadId: string): void {
	awaitingOpenings.add(threadId);
}

/** Consume the one live server opening that belongs to this browser's pick. */
export function consumeFirstRunOpening(threadId: string, message: any): boolean {
	if (!awaitingOpenings.has(threadId) || message?.metadata?.first_run_opening !== true) return false;
	awaitingOpenings.delete(threadId);
	return true;
}

export function cancelFirstRunOpening(threadId: string): void {
	awaitingOpenings.delete(threadId);
}

/** The transcript filters retained rows by thread before holding later output.
 * Stores and the model turn continue normally; the existing reveal timer alone
 * releases presentation, including when navigation finishes it immediately.
 */
export function firstRunOpeningPresentation(messages: Message[], threadId: string | null | undefined) {
	const rows = messages.filter(message => message.thread_id === threadId);
	const opening = rows.find(message => message.metadata?.first_run_opening === true && message.typewriterPos != null);
	return { rows: opening ? rows.filter(message => compareMessageOrder(message, opening) <= 0) : rows, opening };
}

export function _resetFirstRunOpeningRevealForTests(): void {
	awaitingOpenings.clear();
}
