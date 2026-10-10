import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setArtifactPinned } from '$lib/api/docs';
import { toasts } from '$lib/stores/toast';
import { togglePin } from './artifactPanePin';

vi.mock('$lib/api/docs', () => ({
	setArtifactPinned: vi.fn(),
}));

function clearToasts() {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
}

beforeEach(() => {
	clearToasts();
	vi.mocked(setArtifactPinned).mockReset().mockResolvedValue({} as never);
});

afterEach(clearToasts);

describe('togglePin', () => {
	it('pins through the API and reloads the pane', async () => {
		const reload = vi.fn().mockResolvedValue(undefined);

		await togglePin('artifact-1', 'thread-1', true, reload);

		expect(setArtifactPinned).toHaveBeenCalledWith('artifact-1', 'thread-1', true);
		expect(reload).toHaveBeenCalledTimes(1);
	});

	it('unpins through the same API', async () => {
		await togglePin('artifact-1', 'thread-1', false, vi.fn());

		expect(setArtifactPinned).toHaveBeenCalledWith('artifact-1', 'thread-1', false);
	});

	it('stays quiet on success — the filled pin is its own confirmation', async () => {
		await togglePin('artifact-1', 'thread-1', true, vi.fn());

		expect(get(toasts)).toHaveLength(0);
	});

	it('reports a pin failure without reloading', async () => {
		vi.mocked(setArtifactPinned).mockRejectedValueOnce(new Error('pin failed'));
		const reload = vi.fn();

		await togglePin('artifact-1', 'thread-1', true, reload);

		expect(get(toasts)[0]?.message).toBe("Couldn't pin");
		expect(reload).not.toHaveBeenCalled();
	});

	it('names the unpin direction when unpinning fails', async () => {
		vi.mocked(setArtifactPinned).mockRejectedValueOnce(new Error('unpin failed'));

		await togglePin('artifact-1', 'thread-1', false, vi.fn());

		expect(get(toasts)[0]?.message).toBe("Couldn't unpin");
	});
});
