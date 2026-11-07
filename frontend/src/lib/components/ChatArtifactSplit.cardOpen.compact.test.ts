import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Same mock shape as ChatArtifactSplit.cardOpen.test.ts, which measures no canvas at
// all and so runs wide: this one puts the identical artifact-card path on a measured
// compact canvas, the width where the surface the child selected is the only thing
// that puts it on screen.
vi.mock('./ChatPanel.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/ChatPanelWithCard.svelte')).default }));
vi.mock('./ArtifactLeaf.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/LeafModeProbe.svelte')).default }));
vi.mock('./ArtifactTabSheet.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./CanvasSplitDivider.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));

import ChatArtifactSplit from './ChatArtifactSplit.svelte';
import { COMPACT_CANVAS, measureCanvasAs } from '$lib/__tests__/fixtures/measuredCanvas';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	threadWorkspaces.set({});
	measureCanvasAs(COMPACT_CANVAS);
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
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('an artifact card inside a compact chat column', () => {
	it('keeps the artifact on the canvas, because the row already contains its own click', () => {
		expect(host.querySelector('.split')!.classList.contains('compact')).toBe(true);

		host.querySelector<HTMLButtonElement>('button.tile')!.click();
		flushSync();

		const workspace = get(threadWorkspaces).t1;
		expect(workspace.tabs.map((tab) => tab.artifactId)).toEqual(['a1']);
		expect(workspace.activeSurface, 'a card click must not be turned into chat focus').toBe('artifact');
		expect(host.querySelector('.leaf-col'), 'the artifact takes the compact canvas').not.toBeNull();
		expect(host.querySelector('.leaf-probe')!.getAttribute('data-leaf-mode')).toBe('compact');
	});
});
