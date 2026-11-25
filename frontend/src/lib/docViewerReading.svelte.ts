import { untrack } from 'svelte';

/** A document opening owns its input mode; media changes cannot rewrite that choice. */
export function createDocumentReading(context: {
	opening: () => string | null;
	offScreen: () => boolean;
	available: () => boolean;
	clearSelection: () => void;
}) {
	let touch = $state(false);
	let editing = $state(false);
	$effect(() => {
		context.opening();
		untrack(() => {
			touch = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
			editing = false;
		});
	});
	function finish() {
		if (!touch || !editing) return;
		context.clearSelection();
		editing = false;
	}
	$effect(() => { if (context.offScreen()) untrack(finish); });
	$effect(() => {
		const hide = () => { if (document.visibilityState === 'hidden') finish(); };
		document.addEventListener('visibilitychange', hide);
		return () => document.removeEventListener('visibilitychange', hide);
	});
	return {
		get touch() { return touch; },
		get editing() { return editing; },
		get reading() { return touch && !editing; },
		enter() {
			if (!touch || !context.available()) return;
			context.clearSelection();
			editing = true;
		},
		finish,
	};
}
