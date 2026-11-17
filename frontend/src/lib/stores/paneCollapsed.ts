import { writable } from 'svelte/store';

export const paneCollapsed = writable(false);

/** The pane's keyboard shortcut, Cmd-\ (Ctrl-\ off the Mac). */
export const isPaneShortcut = (event: Event): boolean => event instanceof KeyboardEvent && (event.metaKey || event.ctrlKey) && event.key === '\\';

/** The shell's own controls (the sidebar and panel toggles): a press on one is not an outside press for a surface that is up. */
export const isShellControl = (target: EventTarget | null): boolean => target instanceof Element && !!target.closest('[data-shell-control]');

/** While true the pane takes a new width at once instead of easing to it. */
export const paneSnapping = writable(false);

let snaps = 0;

/** Collapses or expands the pane; `instant` skips its width transition for this one change. */
export function setPaneCollapsed(collapsed: boolean, instant = false): void {
	// An eased change right after a snap still eases.
	if (instant) snapShell(); else { snaps++; paneSnapping.set(false); }
	paneCollapsed.set(collapsed);
}

/** The pane and the sidebar take their next width at once. Lifted once a frame has been laid out at it, so the user's own toggles still ease. */
export function snapShell(): void {
	const snap = ++snaps;
	paneSnapping.set(true);
	if (typeof requestAnimationFrame === 'function') {
		requestAnimationFrame(() => requestAnimationFrame(() => { if (snap === snaps) paneSnapping.set(false); }));
	} else paneSnapping.set(false);
}
