import { tick } from 'svelte';

import { focusStillOurs } from '$lib/actions/focusTrap';
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
	if (!focusStillOurs(intent.control, document.activeElement)) return;

	await tick();
	if (!isCurrentThread()) return;
	if (!focusStillOurs(intent.control, document.activeElement)) return;
	document.querySelector<HTMLElement>('[role="textbox"][aria-label="Message CORSIA"]')?.focus();
}
