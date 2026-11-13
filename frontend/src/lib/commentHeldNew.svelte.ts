type Unread = { newComment?: boolean; newReplyIds?: string[] };

/**
 * What a mounted thread keeps of its New signs once the server clears them. Every card keeps its replies' collapse point (otherwise
 * they would disappear from under the reader when the viewed stamp lands); the thread's own New marker stays too, but only on an
 * opened card, which stamps it read at once and closes when the reader is done: `holds` is false for a card that stays (a margin
 * card, the side list), whose New marker shows what the server says. Call it from a component's setup.
 */
export function createHeldNew(comment: () => Unread, holds: () => boolean) {
	const seenReplies = new Set<string>();
	const replyIds = $derived.by(() => {
		for (const id of comment().newReplyIds ?? []) seenReplies.add(id);
		return new Set(seenReplies);
	});
	const newReplyIds = $derived(holds() ? replyIds : new Set(comment().newReplyIds ?? []));
	let commentSeen = $state(false);
	$effect(() => { if (holds() && comment().newComment) commentSeen = true; });
	return {
		/** Every reply that was new while this thread was mounted: where its replies collapse from. */
		get replyIds() { return replyIds; },
		/** The replies that carry a New sign: those that were new while an opened card was up, else what the server says. */
		get newReplyIds() { return newReplyIds; },
		/** The thread's New marker is shown: it is new now, or was while open. */
		get showsNew() { return !!comment().newComment || (holds() && commentSeen); },
	};
}
