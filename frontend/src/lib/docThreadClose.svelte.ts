import { tick, untrack } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';

/**
 * Closes a phone thread sheet when the person's own Resolve takes its thread from open to resolved (Agree never closes it): nothing is left in it
 * to act on, and the resolved list is one tap away. Someone else resolving the thread while it is read leaves the sheet up, and an
 * unsent reply keeps it up so its text is not lost. Call `resolving(id, answered)` from the person's own Resolve only, with the promise of its
 * answer: the mark lasts until that request is answered, so a refused Resolve, or one that settles with the thread still open, leaves
 * nothing behind to close the sheet on someone else's later resolve. Agree never marks. Call during component setup.
 */
export function closeOnOwnResolve(shown: () => DocComment | undefined, hasDraft: (id: string) => boolean, close: () => void) {
	let actedOn: string | null = null;
	let was: { id: string; open: boolean } | null = null;
	$effect(() => {
		const head = shown();
		if (!head) { was = null; actedOn = null; return; }
		const open = !head.resolved;
		const closing = actedOn === head.id && was?.id === head.id && was.open && !open && !untrack(() => hasDraft(head.id));
		was = { id: head.id, open };
		if (closing) { actedOn = null; close(); }
	});
	return {
		resolving: (id: string, answered: Promise<unknown>) => {
			actedOn = id;
			// Everything the answer changes has been seen by the effect after a tick, so a mark still standing then belongs to a Resolve that did not close the thread.
			const settle = () => tick().then(() => { if (actedOn === id) actedOn = null; });
			void answered.then(settle, settle);
		},
	};
}
