import type { DocComment } from '$lib/stores/docComments';

/**
 * The one drawer for a document's comments: the resolved threads, newest first, opened from a text control in the document's
 * header at every width, and only by it: nothing picked, typed or resolved in the document opens it over the cards. It is its
 * own drawer over the document pane, never inside the margin column or the comment sheet. The surface keeps its open state with
 * the viewer, which draws that control. Call it while a component initialises.
 */
export function createResolvedDrawer(inputs: { enabled: () => boolean; resolved: () => DocComment[]; open: () => boolean; setOpen: (open: boolean) => void; setCount: (count: number) => void; entry: () => HTMLElement | null }) {
	/** Mounted from opening until its close has finished, so it leaves the way it came. */
	let mounted = $state(false);
	$effect(() => inputs.setCount(inputs.enabled() ? inputs.resolved().length : 0));
	$effect(() => { if (!inputs.enabled() || inputs.resolved().length === 0) inputs.setOpen(false); });
	$effect(() => { if (inputs.open()) mounted = true; });
	return {
		get mounted() { return mounted; },
		close() { inputs.setOpen(false); },
		/** Its close has finished: focus left with it goes back to the control that opened it. */
		closed() {
			mounted = false;
			const active = document.activeElement;
			if (!active || active === document.body) inputs.entry()?.focus();
		},
	};
}
