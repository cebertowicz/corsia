import { writable } from 'svelte/store';

export const paneCollapsed = writable(false);

/** While true the pane takes a new width at once instead of easing to it. */
export const paneSnapping = writable(false);

let snaps = 0;

/** Collapses or expands the pane; `instant` skips its width transition for this one change. */
export function setPaneCollapsed(collapsed: boolean, instant = false): void {
	const snap = ++snaps;
	// An eased change right after a snap still eases.
	paneSnapping.set(instant);
	if (instant) {
		// Lifted once a frame has been laid out at the new width, so the user's own toggles still ease.
		if (typeof requestAnimationFrame === 'function') {
			requestAnimationFrame(() => requestAnimationFrame(() => { if (snap === snaps) paneSnapping.set(false); }));
		} else paneSnapping.set(false);
	}
	paneCollapsed.set(collapsed);
}
