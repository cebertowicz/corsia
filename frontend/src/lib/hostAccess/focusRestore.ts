import { tick } from 'svelte';

import type { HostAccessLeaseDecision } from '$lib/api/hostAccess';

export type FocusRestoreIntent = {
	leaseId: string;
	threadId: string;
	control: Element;
};

export function focusRestoreIntentForDecision(
	decision: HostAccessLeaseDecision,
	leaseId: string,
	threadId: string,
	control: Element | null,
): FocusRestoreIntent | null {
	if ((decision !== 'revoke' && decision !== 'reject') || !control) return null;
	return { leaseId, threadId, control };
}

export async function restoreComposerFocusAfterLeaseRemoval(
	intent: FocusRestoreIntent,
	isCurrentThread: () => boolean,
): Promise<void> {
	const activeElement = document.activeElement;
	const focusWasAbandoned = activeElement === null
		|| activeElement === document.body
		|| !activeElement.isConnected
		|| intent.control.contains(activeElement);
	if (!focusWasAbandoned) return;

	await tick();
	if (!isCurrentThread()) return;
	const currentFocus = document.activeElement;
	if (
		currentFocus !== null
		&& currentFocus !== document.body
		&& currentFocus.isConnected
		&& !intent.control.contains(currentFocus)
	) return;
	document.querySelector<HTMLElement>('[role="textbox"][aria-label="Message CORSIA"]')?.focus();
}
