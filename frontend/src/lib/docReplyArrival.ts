import { tick } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';

/** A chip arrival reads a thread once its card is visible in a comments list. */
export function createReplyArrival(artifactId: () => string | null, mark: (artifact: string, root: string, item: string) => Promise<void>) {
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
	function whenShown(thread: DocComment) {
		cancel();
		const current = ++arrival;
		void tick().then(() => {
			if (current !== arrival) return;
			const lists = [...document.querySelectorAll<HTMLElement>('.drawer-comments')];
			if (!lists.length) return;
			let card: HTMLElement | undefined;
			const visibility = new IntersectionObserver((entries) => {
				if (current === arrival && entries.some((entry) => entry.isIntersecting && entry.target.isConnected)) { cancel(); stamp(thread); }
			});
			const look = () => {
				if (!lists.some((list) => list.isConnected)) { cancel(); return; }
				const next = lists.flatMap((list) => [...list.querySelectorAll<HTMLElement>('[data-comment-id]')]).find((item) => item.dataset.commentId === thread.id);
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
	return { whenShown };
}
