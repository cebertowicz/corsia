import type { DocSaveStatus } from './docSaveQueue';

export type DocSaveIndicator = 'idle' | 'saving' | 'saved';

/**
 * The "✓ Saved" ghost badge is a transient cosmetic — the store's status
 * stays 'saved' until the next edit, so the 1400ms auto-hide lives here.
 * Call during component init; the effect is torn down with the component.
 */
export function docSaveGhost(status: () => DocSaveStatus | undefined) {
	let showSavedGhost = $state(false);
	let savedGhostTimer: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		if (status() === 'saved') {
			showSavedGhost = true;
			savedGhostTimer = setTimeout(() => { showSavedGhost = false; }, 1400);
		} else {
			showSavedGhost = false;
		}
		return () => {
			if (savedGhostTimer) { clearTimeout(savedGhostTimer); savedGhostTimer = null; }
		};
	});
	return {
		get current(): DocSaveIndicator {
			return status() === 'saving' ? 'saving' : showSavedGhost ? 'saved' : 'idle';
		},
	};
}
