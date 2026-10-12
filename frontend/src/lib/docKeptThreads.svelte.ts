import { SvelteSet } from 'svelte/reactivity';
import { untrack } from 'svelte';
import { leaveDuration } from '$lib/actions/leaveRow';

/**
 * A reply being typed protects a thread another person resolves. While the text is non-empty the thread stays in the
 * drawer's Open list as a resolved card (its reply field intact); when the reply is sent or cleared it leaves, easing
 * out (`leaving`) instead of vanishing in a frame. With reduced motion there is no leaving window: the card is removed at once and
 * the sheet's own crossfade (settleHeight) carries the change, so the list does not dim twice. Call during component initialisation:
 * it registers a pre-render effect.
 */
export function keptThreads(source: {
	comments: () => Array<{ id: string; resolved: boolean }>;
	drafts: () => Record<string, string>;
	/** Only the Open list drops a kept thread; the other filters list it either way. */
	leavesOnRelease: () => boolean;
	reduced: () => boolean;
}) {
	/** Threads that were open when a reply to them was started. */
	const started = new SvelteSet<string>();
	const leaving = new SvelteSet<string>();
	const typing = (id: string) => !!source.drafts()[id]?.trim();
	/** Resolved by someone else mid-reply, with the reply still unsent. */
	const kept = (comment: { id: string; resolved: boolean }) => comment.resolved && started.has(comment.id) && typing(comment.id);
	const timers = new Set<ReturnType<typeof setTimeout>>();
	let held = new Set<string>();
	// Before the render, so a thread whose reply just went away is marked leaving in the same pass that stops keeping it.
	$effect.pre(() => {
		const comments = source.comments();
		const now = new Set<string>();
		for (const comment of comments) {
			if (!typing(comment.id)) started.delete(comment.id);
			else if (!comment.resolved) started.add(comment.id);
			if (kept(comment)) now.add(comment.id);
		}
		const releases = source.leavesOnRelease();
		const reduced = source.reduced();
		untrack(() => {
			for (const id of held) {
				if (now.has(id) || !releases || reduced || !comments.some((comment) => comment.id === id && comment.resolved)) continue;
				leaving.add(id);
				timers.add(setTimeout(() => leaving.delete(id), leaveDuration()));
			}
			held = now;
		});
	});
	$effect(() => () => timers.forEach(clearTimeout));
	return { kept, leaving };
}
