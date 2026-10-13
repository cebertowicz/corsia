import type { DocSaveStatus } from './docSaveQueue';

export type DocSaveIndicator = 'idle' | 'saving' | 'saved';

/**
 * The "✓ Saved" ghost badge is a transient cosmetic — the store's status
 * stays 'saved' until the next edit, so the 1400ms auto-hide lives here.
 * It shows only when the watched draft turns 'saved' while watched: a switch
 * to another draft, or the first look at one, finds a save that already
 * happened and stays quiet.
 * Call during component init; the effect is torn down with the component.
 */
export function docSaveGhost(status: () => DocSaveStatus | undefined, key: () => string | null) {
	let showSavedGhost = $state(false);
	let savedGhostTimer: ReturnType<typeof setTimeout> | null = null;
	let watched: { key: string | null; status: DocSaveStatus | undefined } | null = null;
	const stopTimer = () => {
		if (savedGhostTimer) { clearTimeout(savedGhostTimer); savedGhostTimer = null; }
	};
	$effect(() => {
		const now = { key: key(), status: status() };
		const sameDraft = watched !== null && watched.key === now.key;
		const turnedSaved = now.status === 'saved' && sameDraft && watched!.status !== 'saved';
		watched = now;
		if (turnedSaved) {
			stopTimer();
			showSavedGhost = true;
			savedGhostTimer = setTimeout(() => { savedGhostTimer = null; showSavedGhost = false; }, 1400);
		} else if (now.status !== 'saved' || !sameDraft) {
			// A re-read of the same saved draft keeps the badge and its timer.
			stopTimer();
			showSavedGhost = false;
		}
	});
	$effect(() => stopTimer);
	return {
		get current(): DocSaveIndicator {
			return status() === 'saving' ? 'saving' : showSavedGhost ? 'saved' : 'idle';
		},
	};
}
