import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./ChatPanel.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./ArtifactLeaf.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./ArtifactTabSheet.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));
vi.mock('./CanvasSplitDivider.svelte', async () => ({ default: (await import('$lib/__tests__/fixtures/EmptyComponent.svelte')).default }));

import ChatArtifactSplit from './ChatArtifactSplit.svelte';
import { NO_DOCUMENT_OPEN } from '$lib/artifactDocumentOpenCapability';
import { displayedDocument, updateDocumentOpenCapability } from '$lib/ws/presence';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	displayedDocument.set(null);
});

function render(threadId: string | null) {
	const props = $state({ threadId, isDraggingOver: false, onOpenLightbox: vi.fn(), onAutoCreateThread: async () => {} });
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(ChatArtifactSplit, { target: host, props });
	flushSync();
	return props;
}

describe('ChatArtifactSplit publishes what its canvas shows', () => {
	it('publishes the thread it displays and forgets it when the view is torn down', () => {
		render('thread-split');
		expect(get(displayedDocument)?.threadId).toBe('thread-split');
		unmount(component!); component = null;
		flushSync();
		expect(get(displayedDocument)).toBeNull();
	});

	it('publishes the new thread on a switch while mounted, never clearing it after', () => {
		const props = render('thread-a');
		const seen: (string | null)[] = [];
		const stop = displayedDocument.subscribe((d) => seen.push(d?.threadId ?? null));
		props.threadId = 'thread-b';
		flushSync();
		stop();
		expect(get(displayedDocument)?.threadId).toBe('thread-b');
		expect(seen.slice(1)).not.toContain(null);
	});

	it('leaves another view\'s published thread alone when it is torn down', () => {
		render('thread-a');
		updateDocumentOpenCapability('thread-other', NO_DOCUMENT_OPEN);
		unmount(component!); component = null;
		flushSync();
		expect(get(displayedDocument)?.threadId).toBe('thread-other');
	});
});
