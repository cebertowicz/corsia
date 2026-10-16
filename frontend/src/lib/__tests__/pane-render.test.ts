import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { paneOpen } from '$lib/stores/paneOpen';
import { emit } from '$lib/ws/events';
import Pane from '$lib/components/Pane.svelte';
import ArtifactPane from '$lib/components/ArtifactPane.svelte';

vi.mock('$lib/api/docs', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/api/docs')>();
	return { ...original, fetchArtifactPane: vi.fn() };
});

import { fetchArtifactPane } from '$lib/api/docs';

const EMPTY_PANE = { nodes: [], needs_attention: [] };

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set(null);
	paneOpen.set(false);
	vi.mocked(fetchArtifactPane).mockReset();
	vi.mocked(fetchArtifactPane).mockResolvedValue(EMPTY_PANE as never);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	paneOpen.set(false);
	vi.restoreAllMocks();
});

describe('Pane rendering', () => {
	it('does not render a pane-owned close button on a mobile drawer', () => {
		Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
		paneOpen.set(true);
		component = mount(Pane, {
			target: host,
			props: { label: 'Workspace', sections: [{ title: 'Roadmap', empty: 'No roadmap yet' }] },
		});
		flushSync();

		expect(host.querySelector('button[aria-label="Close panel"]')).toBeNull();
		expect(get(paneOpen)).toBe(true);
	});
});

describe('ArtifactPane refresh ownership', () => {
	it('ignores artifact and turn events for another thread', async () => {
		token.set('tok');
		component = mount(ArtifactPane, { target: host, props: { threadId: 'owner-123' } });
		flushSync();
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledWith('owner-123'));
		const initialCalls = vi.mocked(fetchArtifactPane).mock.calls.length;

		emit('artifacts_changed', {
			artifact_id: 'artifact-1',
			owner_thread_id: 'other-owner',
			affected_thread_ids: ['other-owner'],
		});
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(fetchArtifactPane).toHaveBeenCalledTimes(initialCalls);

		emit('turn_complete', { thread_id: 'other-owner' });
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(fetchArtifactPane).toHaveBeenCalledTimes(initialCalls);
	});

	it('refreshes when its own artifact or turn changes', async () => {
		token.set('tok');
		component = mount(ArtifactPane, { target: host, props: { threadId: 'owner-123' } });
		flushSync();
		await vi.waitFor(() => expect(fetchArtifactPane).toHaveBeenCalledWith('owner-123'));
		const initialCalls = vi.mocked(fetchArtifactPane).mock.calls.length;

		emit('artifacts_changed', {
			artifact_id: 'artifact-1',
			owner_thread_id: 'owner-123',
			affected_thread_ids: ['owner-123'],
		});
		await vi.waitFor(() => expect(vi.mocked(fetchArtifactPane).mock.calls.length).toBeGreaterThan(initialCalls));
		const afterArtifactRefresh = vi.mocked(fetchArtifactPane).mock.calls.length;

		emit('turn_complete', { thread_id: 'owner-123' });
		await vi.waitFor(() => expect(vi.mocked(fetchArtifactPane).mock.calls.length).toBeGreaterThan(afterArtifactRefresh));
	});
});
