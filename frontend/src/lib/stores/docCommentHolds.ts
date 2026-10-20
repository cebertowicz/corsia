import type { DocComment } from './docComments';

/**
 * Comment arrivals a presence lane is still staging, per document. A load leaves them out of what it shows and
 * keeps the row aside, so the count, drawer and canvas anchors follow the lane. This is only the bookkeeping:
 * `docComments.ts` still writes everything that is shown, and `docCommentArrivals.ts` holds and releases.
 */
const held = new Map<string, Set<string>>();
const withheld = new Map<string, Map<string, DocComment>>();
/** The agent call each held suggestion was made by, so that call's streamed preview can wait out the hold. */
const heldCalls = new Map<string, Map<string, string>>();

export const heldIds = (artifactId: string): string[] => [...(held.get(artifactId) ?? [])];

/** What a load shows: the rows without the held ones, which are kept aside; held replies are left out of their thread. */
export function withhold(artifactId: string, rows: DocComment[]): DocComment[] {
	const ids = held.get(artifactId);
	if (!ids?.size) return rows;
	const stash = withheld.get(artifactId) ?? new Map<string, DocComment>();
	withheld.set(artifactId, stash);
	return rows
		.filter((row) => { if (ids.has(row.id)) stash.set(row.id, row); return !ids.has(row.id); })
		.map((row) => ({ ...row, replies: row.replies.filter((reply) => !reply.id || !ids.has(reply.id)) }));
}

/** Name the call a held comment came from; ignored when the comment is not held. */
export function holdCall(artifactId: string, commentId: string, callId: string): void {
	if (!held.get(artifactId)?.has(commentId)) return;
	heldCalls.set(artifactId, (heldCalls.get(artifactId) ?? new Map()).set(callId, commentId));
}

/** Whether a held comment came from this call. */
export const isCallHeld = (artifactId: string, callId: string): boolean => heldCalls.get(artifactId)?.has(callId) ?? false;

/** Start holding; `shown` is the row already on screen, if any, which is set aside. */
export function startHold(artifactId: string, commentId: string, shown?: DocComment): void {
	held.set(artifactId, (held.get(artifactId) ?? new Set()).add(commentId));
	if (shown) withheld.set(artifactId, (withheld.get(artifactId) ?? new Map()).set(commentId, shown));
}

/** Stop holding. Returns undefined when it was not held, else the row kept aside (null when none was). */
export function endHold(artifactId: string, commentId: string): DocComment | null | undefined {
	const ids = held.get(artifactId);
	if (!ids?.delete(commentId)) return undefined;
	const calls = heldCalls.get(artifactId);
	for (const [callId, held] of calls ?? []) if (held === commentId) calls!.delete(callId);
	if (calls && !calls.size) heldCalls.delete(artifactId);
	if (!ids.size) held.delete(artifactId);
	const stash = withheld.get(artifactId);
	const row = stash?.get(commentId) ?? null;
	stash?.delete(commentId);
	if (stash && !stash.size) withheld.delete(artifactId);
	return row;
}
