import { tick } from 'svelte';

import { focusStillOurs } from '$lib/actions/focusTrap';
import type { HostAccessLeaseDecision } from '$lib/api/hostAccess';

export type FocusRestoreIntent = {
	leaseId: string;
	threadId: string;
	control: Element;
};

export type DecisionTargets = {
	/** The button the person pressed. */
	pressed: HTMLElement | null;
	/** The drawer panel it sits in, which holds the drawer's Escape and Tab. */
	panel: HTMLElement | null;
	/** The surface whose focus may pass to the composer once the request leaves the list. */
	control: Element | null;
};

export function decisionTargets(event: MouseEvent | null, decision: HostAccessLeaseDecision): DecisionTargets {
	const pressed = event?.currentTarget instanceof HTMLElement ? event.currentTarget : null;
	// A Revoke's surface is its whole drawer or chip: the drawer is portaled out of the chip.
	const control = decision === 'revoke' ? pressed?.closest('[data-adaptive-drawer], .active-chip') ?? null : pressed;
	return { pressed, panel: pressed?.closest<HTMLElement>('[role="dialog"]') ?? null, control };
}

/**
 * Reject and Revoke end the request, so focus passes to the composer once it leaves the list. A request
 * already gone (404) has ended whatever the decision, Approve included: its entry is gone too, so nothing
 * else can take focus back.
 */
export function focusRestoreIntentForDecision(
	decision: HostAccessLeaseDecision,
	leaseId: string,
	threadId: string,
	control: Element | null,
	gone = false,
): FocusRestoreIntent | null {
	if ((!gone && decision !== 'revoke' && decision !== 'reject') || !control) return null;
	return { leaseId, threadId, control };
}

/**
 * A failed decision left its drawer open, and the pressed button lost focus while it was disabled. Once it
 * is enabled again focus goes back to it; if it is still disabled or gone, to the drawer panel, so Escape
 * and Tab keep working. Focus the person moved somewhere else is left there.
 */
export async function refocusAfterFailedDecision(
	pressed: HTMLElement | null,
	panel: HTMLElement | null,
): Promise<void> {
	await tick();
	if (!panel?.isConnected || !focusStillOurs(pressed, document.activeElement)) return;
	if (pressed?.isConnected && panel.contains(pressed) && !(pressed as HTMLButtonElement).disabled) pressed.focus();
	else panel.focus();
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
