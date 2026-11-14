import { untrack } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';

/**
 * Closes a phone thread sheet when the person's own Resolve or Agree takes its thread from open to resolved: nothing is left in it
 * to act on, and the resolved list is one tap away. Someone else resolving the thread while it is read leaves the sheet up, and an
 * unsent reply keeps it up so its text is not lost. Call `acted(id)` from the person's own Agree and Resolve; call during component setup.
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
	return { acted: (id: string) => { actedOn = id; } };
}
