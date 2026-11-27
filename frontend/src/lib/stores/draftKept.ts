/**
 * The sentence a failed comment or reply says when its text stays with the person: every reason that says so builds it
 * from this, so it reads the same everywhere.
 */
export const DRAFT_KEPT = 'Your draft is kept.';
/** What a chat comment's box adds to why its send failed: the text stays in that box. A notice keeps no text, so says none. */
export const TEXT_KEPT = 'Your text stays here to copy.';

/** `reason` without `sentence` and the one space beside it, wherever in the reason that sentence sits. */
function without(reason: string, sentence: string): string {
	const at = reason.indexOf(sentence);
	if (at < 0) return reason;
	const before = reason.slice(0, at), after = reason.slice(at + sentence.length);
	if (after.startsWith(' ')) return before + after.slice(1);
	return before.endsWith(' ') ? before.slice(0, -1) + after : before + after;
}
/** `reason` without its `DRAFT_KEPT` sentence. */
export const withoutDraftKept = (reason: string): string => without(reason, DRAFT_KEPT);
/** `reason` without its `TEXT_KEPT` sentence, for a notice away from the box that keeps the text. */
export const withoutTextKept = (reason: string): string => without(reason, TEXT_KEPT);

/**
 * The one line under a comment's box or a reply's field: why its post failed, then that this device does not keep the
 * draft (`notKept`, empty while it does). A reason that says the draft is kept does not say so beside a line saying it is not.
 */
export function failedLine(reason: string, notKept: string): string {
	return [notKept ? withoutDraftKept(reason) : reason, notKept].filter(Boolean).join(' ');
}
