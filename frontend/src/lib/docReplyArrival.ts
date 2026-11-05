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
	function whenShown(thread: DocComment, frames = 120) {
		const shown = () => [...document.querySelectorAll<HTMLElement>('.drawer-comments [data-comment-id]')].filter((card) => card.dataset.commentId === thread.id).some((card) => {
			const list = card.closest('.drawer-comments')!.getBoundingClientRect(), at = card.getBoundingClientRect();
			return at.height > 0 && at.bottom > list.top && at.top < list.bottom;
		});
		const look = (left: number) => { if (shown()) stamp(thread); else if (left > 0) requestAnimationFrame(() => look(left - 1)); };
		void tick().then(() => look(frames));
	}
	return { whenShown };
}
