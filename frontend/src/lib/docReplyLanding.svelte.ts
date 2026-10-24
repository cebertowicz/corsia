import { tick } from 'svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { DocComment } from '$lib/stores/docComments';
import type { CanvasScrollTarget } from '$lib/docCanvasComments';

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
