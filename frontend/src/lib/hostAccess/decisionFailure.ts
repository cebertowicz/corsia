import { HostAccessApiError, type HostAccessItem, type HostAccessLeaseDecision } from '$lib/api/hostAccess';

/** A decision refused because the request changed since it was read (409), for every kind. */
export const NEW_DETAILS = 'This request has new details. Review them, then approve or reject it.';
/** An Approve that failed for any other reason; Approve stays on. */
export const APPROVE_FAILED = 'Couldn’t approve the request. Try again.';

export interface DecisionFailure {
	/** The line the drawer shows, or null when the could-not-check line already says it. */
	notice: string | null;
	/** Read the request list again. */
	refresh: boolean;
	/** The list can no longer be trusted, so Approve turns off until it is read again. */
	stale: boolean;
}

/** What the person sees, and what the control does next, when a decision on a request fails. */
export function decisionFailure(
	caught: unknown,
	decision: HostAccessLeaseDecision,
	kind: HostAccessItem['kind'],
): DecisionFailure {
	const status = caught instanceof HostAccessApiError ? caught.status : null;
	// The request changed since it was read: never the server's words; read it again and say so.
	if (status === 409) return { notice: NEW_DETAILS, refresh: true, stale: false };
	// A failed Approve (a 500, any other refusal, or no connection) leaves Approve on to try again.
	// A fill that is gone keeps its own approved line, below.
	if (decision === 'approve' && !(status === 404 && kind === 'fill')) {
		return { notice: APPROVE_FAILED, refresh: status === 404, stale: false };
	}
	const serverLine = caught instanceof HostAccessApiError ? caught.message : null;
	// Gone: the server's line, then the list without it.
	if (status === 404) return { notice: serverLine, refresh: true, stale: false };
	// Only the server's own words: a dropped connection already shows the could-not-check line.
	return { notice: serverLine, refresh: false, stale: true };
}
