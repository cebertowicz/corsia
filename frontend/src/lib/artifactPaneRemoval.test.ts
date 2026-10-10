import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setArtifactDisplay } from '$lib/api/docs';
import { toasts } from '$lib/stores/toast';
import { removeArtifactWithUndo } from './artifactPaneRemoval';

vi.mock('$lib/api/docs', () => ({
	setArtifactDisplay: vi.fn(),
}));

function clearToasts() {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
}

beforeEach(() => {
	clearToasts();
	vi.mocked(setArtifactDisplay).mockReset().mockResolvedValue({} as never);
});

afterEach(clearToasts);

describe('removeArtifactWithUndo', () => {
	it('offers Undo after removal and restores through the same display API', async () => {
		const reload = vi.fn().mockResolvedValue(undefined);

		await removeArtifactWithUndo('artifact-1', 'thread-1', reload);

		const toast = get(toasts)[0]!;
		expect(toast.message).toBe('Removed from pane');
		expect(toast.action?.label).toBe('Undo');
		await toast.action?.run();

		expect(setArtifactDisplay).toHaveBeenNthCalledWith(
			1, 'artifact-1', 'thread-1', true,
		);
		expect(setArtifactDisplay).toHaveBeenNthCalledWith(
			2, 'artifact-1', 'thread-1', false,
		);
		expect(reload).toHaveBeenCalledTimes(2);
	});

	it('reports a restore failure while leaving the removal intact', async () => {
		vi.mocked(setArtifactDisplay)
			.mockResolvedValueOnce({} as never)
			.mockRejectedValueOnce(new Error('restore failed'));

		await removeArtifactWithUndo('artifact-1', 'thread-1', vi.fn());
		await get(toasts)[0]?.action?.run();

		expect(get(toasts).some(
			(toast) => toast.message === "Couldn't restore artifact",
		)).toBe(true);
	});

	it('shows only the existing error toast when removal fails', async () => {
		vi.mocked(setArtifactDisplay).mockRejectedValueOnce(new Error('remove failed'));

		await removeArtifactWithUndo('artifact-1', 'thread-1', vi.fn());

		expect(get(toasts)).toHaveLength(1);
		expect(get(toasts)[0]?.message).toBe("Couldn't remove from pane");
		expect(get(toasts)[0]?.action).toBeUndefined();
	});
});
