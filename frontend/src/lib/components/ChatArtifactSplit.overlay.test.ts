import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('./ChatPanel.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/ChatPanelWithCard.svelte')).default }));
vi.mock('./ArtifactLeaf.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/LeafModeProbe.svelte')).default }));
vi.mock('./ArtifactTabSheet.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./CanvasSplitDivider.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));

import ChatArtifactSplit from './ChatArtifactSplit.svelte';
import { measureCanvasAs } from '$lib/__tests__/fixtures/measuredCanvas';
import { paneOverlay } from '$lib/stores/artifactCanvas';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => {
	if (component) unmount(component);
	host?.remove();
	paneOverlay.set(false);
	threadWorkspaces.set({});
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

it('uses the unobscured width for compact layout and restores the split after the overlay closes', () => {
	measureCanvasAs(1000);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ChatArtifactSplit, { target: host, props: { threadId: 't1', isDraggingOver: false, onOpenLightbox: vi.fn(), onAutoCreateThread: async () => {} } });
	flushSync();
	host.querySelector<HTMLButtonElement>('button.tile')!.click();
	flushSync();
	expect(host.querySelector('.split')!.classList.contains('compact')).toBe(false);
	paneOverlay.set(true);
	flushSync();
	expect(host.querySelector('.split')!.classList.contains('compact')).toBe(true);
	expect(host.querySelector('.leaf-probe')!.getAttribute('data-leaf-mode')).toBe('compact');
	paneOverlay.set(false);
	flushSync();
	expect(host.querySelector('.split')!.classList.contains('compact')).toBe(false);
	expect(host.querySelector('.leaf-probe')!.getAttribute('data-leaf-mode')).toBe('expanded');
});
