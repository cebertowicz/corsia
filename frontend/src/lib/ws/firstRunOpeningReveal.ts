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

export function _resetFirstRunOpeningRevealForTests(): void {
	awaitingOpenings.clear();
}
