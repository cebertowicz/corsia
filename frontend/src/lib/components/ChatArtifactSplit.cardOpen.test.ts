import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./ChatPanel.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/ChatPanelWithCard.svelte')).default }));
vi.mock('./ArtifactLeaf.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./ArtifactTabSheet.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./CanvasSplitDivider.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));

import ChatArtifactSplit from './ChatArtifactSplit.svelte';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	threadWorkspaces.set({});
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ChatArtifactSplit, {
		target: host,
		props: { threadId: 't1', isDraggingOver: false, onOpenLightbox: vi.fn(), onAutoCreateThread: async () => {} },
	});
	flushSync();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('an artifact card inside the chat column', () => {
	it('leaves the artifact as the active surface once it is opened', () => {
		host.querySelector<HTMLButtonElement>('button.tile')!.click();
		flushSync();

		const workspace = get(threadWorkspaces).t1;
		expect(workspace.tabs.map((tab) => tab.artifactId)).toEqual(['a1']);
		// The click then bubbles to the chat column, which marks chat active: it must not.
		expect(workspace.activeSurface).toBe('artifact');
	});
});
