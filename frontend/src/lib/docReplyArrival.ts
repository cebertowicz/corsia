import { tick } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';

/** The comment surfaces a card can be shown in: the list, the card beside the text, and the phone sheet. */
const SURFACES = '.drawer-comments, [data-popover], [data-adaptive-drawer]';

/** An arrival (the chip, the notice, a margin marker) reads a thread once its card is visible on a comment surface. */
export function createReplyArrival(artifactId: () => string | null, comments: () => DocComment[], mark: (artifact: string, root: string, item: string) => Promise<void>) {
	const stamped = new Set<string>();
	function stamp(thread: DocComment) {
		const unread = new Set(thread.newReplyIds ?? []);
		const last = [...thread.replies].reverse().find((reply) => reply.id && unread.has(reply.id))?.id ?? (thread.newComment ? thread.id : undefined);
		const artifact = artifactId();
		if (!artifact || !last || stamped.has(last)) return;
		stamped.add(last);
		void mark(artifact, thread.id, last).catch((err) => { stamped.delete(last); console.warn('Could not record the viewed reply', err); });
	}
	let cancel = () => {};
	let arrival = 0;
	function whenShown(id: string) {
		const thread = comments().find((comment) => comment.id === id);
		if (!thread) return;
		cancel();
		const current = ++arrival;
		void tick().then(() => {
			if (current !== arrival) return;
			// The card beside the text mounts after this tick, so surfaces are found as they appear; once one has been
			// open, closing every surface ends the wait.
			let opened = false;
			let card: HTMLElement | undefined;
			const visibility = new IntersectionObserver((entries) => {
				if (current === arrival && entries.some((entry) => entry.isIntersecting && entry.target.isConnected)) { cancel(); stamp(thread); }
			});
			const look = () => {
				const lists = [...document.querySelectorAll<HTMLElement>(SURFACES)];
				if (lists.length) opened = true;
				else if (opened) { cancel(); return; }
				const next = lists.flatMap((list) => [...list.querySelectorAll<HTMLElement>('[data-thread-card]')]).find((item) => item.dataset.threadCard === thread.id);
				if (next === card) return;
				if (card) visibility.unobserve(card);
				card = next;
				if (card) visibility.observe(card);
			};
			const changes = new MutationObserver(look);
			cancel = () => { visibility.disconnect(); changes.disconnect(); cancel = () => {}; };
			changes.observe(document.body, { childList: true, subtree: true });
			look();
		});
	}
	// Until a surface opens the wait watches the whole page, so the viewer ends it when it goes away.
	return { whenShown, stop: () => { arrival++; cancel(); } };
}
