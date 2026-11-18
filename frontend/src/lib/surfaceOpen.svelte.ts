import { getContext, setContext } from 'svelte';

const KEY = Symbol('surface-open');

/**
 * A surface that opens and closes over its content (a sheet, a popover) counts its opens for what it holds. Opened
 * again while it is still closing, it keeps the same elements, so what runs once per open keys on this count, not on
 * the element: the count steps each time the surface goes from closed to open.
 */
export function provideSurfaceOpen(isOpen: () => boolean): void {
	let was = isOpen();
	let count = $state(was ? 1 : 0);
	$effect.pre(() => {
		const now = isOpen();
		if (now && !was) count += 1;
		was = now;
	});
	setContext(KEY, () => count);
}

/** The nearest surface's open count, read reactively; 0 where no surface counts its opens. */
export function surfaceOpen(): () => number {
	return getContext<(() => number) | undefined>(KEY) ?? (() => 0);
}
