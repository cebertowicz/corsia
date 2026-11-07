import { tick } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';

/**
 * The box under the margin column that lists the resolved threads (or the review conversations, which never had any
 * text). Call it while a component initialises.
 */
export function createMarginAside(inputs: { margin: () => boolean; selectedId: () => string | null; resolved: () => DocComment[]; reviews: () => DocComment[] }) {
	let open = $state(false);
	/** What the open list in the column holds: the resolved threads, or the review conversations (which never had any text). */
	let kind = $state<'resolved' | 'review'>('resolved');
	let entry = $state<HTMLElement>();
	/** Where the open list starts: directly under its entry, which stays whole above it. */
	let top = $state(0);
	$effect(() => {
		if (!open || !entry || typeof ResizeObserver === 'undefined') return;
		const row = entry;
		const read = () => top = row.offsetTop + row.offsetHeight;
		read(); const observer = new ResizeObserver(read); observer.observe(row);
		return () => observer.disconnect();
	});
	// A thread picked in the column or the text is in front of the person: the list, which covers the column, steps aside unless the pick is one of its own.
	$effect(() => {
		const id = inputs.selectedId();
		if (!inputs.margin() || !id) return;
		const found = inputs.resolved().some((comment) => comment.id === id) ? 'resolved' : inputs.reviews().some((comment) => comment.id === id) ? 'review' : null;
		if (!found) { open = false; return; }
		kind = found;
		open = true;
		void tick().then(() => document.querySelector<HTMLElement>(`.margin-aside [data-comment-id="${id}"]`)?.scrollIntoView?.({ block: 'nearest' }));
	});
	return {
		get open() { return open; },
		set open(value: boolean) { open = value; },
		get kind() { return kind; },
		set kind(value: 'resolved' | 'review') { kind = value; },
		get entry() { return entry; },
		set entry(value: HTMLElement | undefined) { entry = value; },
		get top() { return top; },
		/** Closing the box by its own control or Escape hands focus back to the entry that opened it, and leaves the column open. */
		close() {
			const shown = kind;
			open = false;
			void tick().then(() => entry?.querySelector<HTMLElement>(`[data-aside-kind="${shown}"]`)?.focus());
		},
	};
}
