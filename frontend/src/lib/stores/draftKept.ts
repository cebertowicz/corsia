/**
 * The sentence a failed comment or reply says when its text stays with the person: every reason that says so builds it
 * from this, so it reads the same everywhere.
 */
export const DRAFT_KEPT = 'Your draft is kept.';

/** `reason` without its `DRAFT_KEPT` sentence and the one space beside it, wherever in the reason that sentence sits. */
export function withoutDraftKept(reason: string): string {
	const at = reason.indexOf(DRAFT_KEPT);
	if (at < 0) return reason;
	const before = reason.slice(0, at), after = reason.slice(at + DRAFT_KEPT.length);
	if (after.startsWith(' ')) return before + after.slice(1);
	return before.endsWith(' ') ? before.slice(0, -1) + after : before + after;
}

/**
 * The one line under a comment's box or a reply's field: why its post failed, then that this device does not keep the
 * draft (`notKept`, empty while it does). A reason that says the draft is kept does not say so beside a line saying it is not.
 */
export function failedLine(reason: string, notKept: string): string {
	return [notKept ? withoutDraftKept(reason) : reason, notKept].filter(Boolean).join(' ');
}
