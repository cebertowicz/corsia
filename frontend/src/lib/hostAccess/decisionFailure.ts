import { HostAccessApiError, type HostAccessItem, type HostAccessLeaseDecision } from '$lib/api/hostAccess';

/** A decision refused because the request changed since it was read (409): host access and saved logins. */
export const NEW_DETAILS = 'This request has new details. Review them, then approve or reject it.';
/** The same for a script request, whose refusal is Deny. */
export const NEW_DETAILS_SCRIPT = 'This request has new details. Review them, then approve or deny it.';
/** A Revoke refused because the approval changed since it was read (409). */
export const APPROVAL_CHANGED = 'This approval has changed. Check it, then revoke it if you still need to.';
/** A decision that failed any other way (a refusal, a server error, no connection); its button stays on. */
export const APPROVE_FAILED = 'Couldn’t approve the request. Try again.';
export const REJECT_FAILED = 'Couldn’t reject the request. Try again.';
export const DENY_FAILED = 'Couldn’t deny the request. Try again.';
export const REVOKE_FAILED = 'Couldn’t revoke the approval. Try again.';

export interface DecisionFailure {
	/** The one line the drawer shows for the failed press. */
	notice: string | null;
	/** Read the request list again. */
	refresh: boolean;
}

function changedLine(decision: HostAccessLeaseDecision, kind: HostAccessItem['kind']): string {
	if (decision === 'revoke') return APPROVAL_CHANGED;
	return kind === 'script' ? NEW_DETAILS_SCRIPT : NEW_DETAILS;
}

function failedLine(decision: HostAccessLeaseDecision, kind: HostAccessItem['kind']): string {
	if (decision === 'approve') return APPROVE_FAILED;
	if (decision === 'revoke') return REVOKE_FAILED;
	return kind === 'script' ? DENY_FAILED : REJECT_FAILED;
}

/**
 * What the person sees, and what the control does next, when a decision on a request fails. One line per
 * failed press, never the server's own words except a gone request's; a failed press never marks the list
 * as unchecked, so the could-not-check line stays for its own case.
 */
export function decisionFailure(
	caught: unknown,
	decision: HostAccessLeaseDecision,
	kind: HostAccessItem['kind'],
): DecisionFailure {
	const status = caught instanceof HostAccessApiError ? caught.status : null;
	// The request changed since it was read: say so and read it again.
	if (status === 409) return { notice: changedLine(decision, kind), refresh: true };
	// Gone: the server's line for a closed request, then the list without it. A gone host-access or script
	// request on Approve says Approve failed and reads again.
	if (status === 404) {
		if (decision === 'approve' && kind !== 'fill') return { notice: APPROVE_FAILED, refresh: true };
		const serverLine = caught instanceof HostAccessApiError ? caught.message : null;
		return { notice: serverLine, refresh: true };
	}
	// Any other refusal, a server error or no connection: the button stays on to try again.
	return { notice: failedLine(decision, kind), refresh: false };
}
