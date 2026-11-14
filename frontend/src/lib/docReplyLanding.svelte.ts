import { tick } from 'svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { hasUnread, type DocComment } from '$lib/stores/docComments';
import { canvasCommentScrollTarget, type CanvasScrollTarget } from '$lib/docCanvasComments';

/**
 * The document surface's jump landing: the one way a notice (the jump control, or an arrival landing) moves the reader to
 * an unread thread. Its text is scrolled to, its card opened, and a comment on an image opens the image at its pin.
 */
export function createReplyNavigation(source: {
	target: () => DocViewerTarget | null;
	ready: () => boolean;
	comments: () => DocComment[];
	text: () => string;
	/** `picked`: the reader chose this thread (the jump control), so showing it reads it; a tab's restore landing does not. */
	show: (thread: DocComment | null, landing: CanvasScrollTarget | null, picked: boolean) => void;
	openImageOr: (id: string, otherwise: () => void) => void;
	onLanded: () => void;
}) {
	const first = () => source.comments().find(hasUnread) ?? null;
	const landing = (thread: DocComment | null) => thread
		? canvasCommentScrollTarget(source.comments(), source.text(), thread.id, false, 'quote-tap') : null;
	createReplyLanding({
		target: source.target, ready: source.ready, first, landing: () => landing(first()),
		open: (thread, target) => { source.show(thread, target, false); source.onLanded(); },
	});
	/** Goes to one thread the reader chose: the nearest the jump control is pointing at, wherever it sits in the document. */
	function goTo(id: string, beforeText?: () => void) {
		const thread = source.comments().find((item) => item.id === id);
		if (!thread) return;
		const target = landing(thread);
		source.openImageOr(thread.id, () => { beforeText?.(); source.show(thread, target, true); });
	}
	return { goTo };
}

export function createReplyLanding(source: {
	target: () => DocViewerTarget | null;
	ready: () => boolean;
	first: () => DocComment | null;
	landing: () => CanvasScrollTarget | null;
	open: (first: DocComment | null, landing: CanvasScrollTarget | null) => void;
}) {
	let arrivedAt = $state<string | null>(null);
	// A tab opened from a waiting reply lands once on the first unread root in document order,
	// after the comments have loaded (an empty list mid-load would spend the landing on nothing).
	// Ordinary document opens leave the comments surface alone.
	$effect(() => {
		const target = source.target();
		const instance = target?.instanceId;
		if (target?.landOn !== 'new-replies' || !source.ready() || !instance || arrivedAt === instance) return;
		const first = source.first();
		const landing = source.landing();
		// A comment load can finish before its anchors are placed in the document. Do not spend this one-shot
		// landing on a null target: wait for the first unread root to become scrollable, then land on its text.
		if (first?.placement === 'placing' && !landing) return;
		arrivedAt = instance;
		void tick().then(() => source.open(first, landing));
	});
}
