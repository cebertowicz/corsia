import { setArtifactDisplay } from '$lib/api/docs';
import { toasts } from '$lib/stores/toast';

export async function removeArtifactWithUndo(
	artifactId: string,
	threadId: string,
	reload: () => void | Promise<void>,
): Promise<void> {
	try {
		await setArtifactDisplay(artifactId, threadId, true);
		await reload();
	} catch {
		toasts.show("Couldn't remove from pane", 'error');
		return;
	}

	toasts.showAction('Removed from pane', {
		label: 'Undo',
		run: async () => {
			try {
				await setArtifactDisplay(artifactId, threadId, false);
				await reload();
			} catch {
				toasts.show("Couldn't restore artifact", 'error');
			}
		},
	});
}
