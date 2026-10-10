import { setArtifactPinned } from '$lib/api/docs';
import { toasts } from '$lib/stores/toast';

/**
 * Pin or unpin one artifact, then reload the pane.
 *
 * Pinning is its own undo — the row stays on screen with the pin filled in, and
 * the same control reverses it — so this reports only failure, unlike removal.
 */
export async function togglePin(
	artifactId: string,
	threadId: string,
	pinned: boolean,
	reload: () => void | Promise<void>,
): Promise<void> {
	try {
		await setArtifactPinned(artifactId, threadId, pinned);
		await reload();
	} catch {
		toasts.show(pinned ? "Couldn't pin" : "Couldn't unpin", 'error');
	}
}
