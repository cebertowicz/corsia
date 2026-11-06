// Nothing has been measured yet, so nothing is known to be unplaced: a message renders no detached
// thread until the surface that paints its highlights says it could not place one.
let unplaced = $state<Record<string, string[]>>({});
const NONE: string[] = [];

/**
 * The sent chat comments that got no highlight, by message id.
 *
 * `ChatCommentSelection` paints the highlights and is the only actor that can: a highlight needs the
 * comment's quote to sit at its offsets in the *rendered* message text, which is a DOM measurement.
 * A comment whose text no longer matches gets no highlight, so the message row shows it as a detached
 * thread beneath the message instead — the comment always has a surface, and its replies, its unread
 * mark and the jump that lands on it all work there. Each surface records only the messages it
 * painted and clears them when that chat closes.
 */
export const chatCommentPlacement = {
	/** Note which comments of one message got no highlight. An unchanged set leaves readers alone. */
	record(messageId: string, commentIds: string[]): void {
		const current = unplaced[messageId];
		if (!current) { if (!commentIds.length) return; }
		else if (current.length === commentIds.length && current.every((id, index) => id === commentIds[index])) return;
		if (commentIds.length) unplaced[messageId] = commentIds;
		else delete unplaced[messageId];
	},
	/** The ids of this message's comments with no highlight, to show beneath it. */
	unplacedIds(messageId: string): string[] {
		return unplaced[messageId] ?? NONE;
	},
	/** Clear what one transcript surface recorded. */
	forget(messageIds: string[]): void {
		for (const messageId of messageIds) delete unplaced[messageId];
	},
};
