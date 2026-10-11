import { get } from 'svelte/store';
import { paneCollapsed } from '$lib/stores/paneCollapsed';
import { isPaneDrawer, paneOpen } from '$lib/stores/paneOpen';

/** The leaf width the workspace pane's collapse gives (artifactSplitLayout's default cap); at or above it there is nothing to gain. */
export const WIDE_ENOUGH_LEAF = 600;

/**
 * Gives an open image viewer the width of a collapsed workspace pane, and returns the way to hand it back.
 *
 * It changes only what the user did not already have: a pane that was collapsed stays so, and only what it
 * changed is restored. It does nothing at drawer widths (the pane is a drawer there), nor when the user has
 * already made the leaf wide. If the user toggles the pane while the viewer is open their choice wins: nothing
 * is restored. The returned function is safe to call more than once.
 */
export function widenForImageViewer(leafWidth: number): () => void {
	if (isPaneDrawer() || leafWidth >= WIDE_ENOUGH_LEAF) return () => {};
	const before = { collapsed: get(paneCollapsed), open: get(paneOpen) };
	const changed = { collapsed: !before.collapsed, open: before.open };
	if (!changed.collapsed && !changed.open) return () => {};

	let ours = true;
	let userChoice = false;
	const watch = (expected: () => boolean) => (value: boolean) => {
		if (ours || value === expected()) return;
		userChoice = true;
	};
	const unsubscribers = [
		paneCollapsed.subscribe(watch(() => true)),
		paneOpen.subscribe(watch(() => false)),
	];
	if (changed.collapsed) paneCollapsed.set(true);
	if (changed.open) paneOpen.set(false);
	ours = false;

	let released = false;
	return () => {
		if (released) return;
		released = true;
		for (const stop of unsubscribers) stop();
		if (userChoice) return;
		if (changed.collapsed) paneCollapsed.set(false);
		if (changed.open) paneOpen.set(true);
	};
}
