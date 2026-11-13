type Unread = { newComment?: boolean; newReplyIds?: string[] };

/**
 * What a mounted thread keeps of its New signs once the server clears them. Replies keep their collapse point (otherwise they would
 * disappear from under the reader when the viewed stamp lands); the thread's own New marker stays too, because opening the card
 * stamps it read at once and the sign the reader came to see stays until the card is closed. Only an opened card closes: `holds` is
 * false for a card that stays (a margin card, the side list), which shows what the server says. Call it from a component's setup.
 */
export function createHeldNew(comment: () => Unread, holds: () => boolean) {
	const seenReplies = new Set<string>();
	const replyIds = $derived.by(() => {
		const now = comment().newReplyIds ?? [];
		if (!holds()) return new Set(now);
		for (const id of now) seenReplies.add(id);
		return new Set(seenReplies);
	});
	let commentSeen = $state(false);
	$effect(() => { if (holds() && comment().newComment) commentSeen = true; });
	return {
		/** The replies that were new while this thread was open. */
		get replyIds() { return replyIds; },
		/** The thread's New marker is shown: it is new now, or was while open. */
		get showsNew() { return !!comment().newComment || (holds() && commentSeen); },
	};
}
