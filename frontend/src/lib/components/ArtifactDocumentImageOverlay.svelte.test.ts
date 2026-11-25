import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./ImageCommentLightbox.svelte', async () => ({ default: (await import('./ArtifactDocumentImageOverlayStub.test.svelte')).default }));

import ArtifactDocumentImageOverlay from './ArtifactDocumentImageOverlay.svelte';
import { openTabDocumentImage, tabDocumentImage } from '$lib/stores/tabViewState';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const HOST = 'leaf-thread';
const DOCUMENT_THREAD = 'document-thread';
const TAB = 'tab-1';
const TARGET: DocViewerTarget = { instanceId: TAB, path: '/ws/a.md', name: 'a.md', projectId: null, threadId: DOCUMENT_THREAD, artifactId: 'art' };
const DRAWN = { src: 'blob:old', alt: 'Diagram', from: 9, to: 40, scrollTop: 0, top: 0, diagramSource: 'old' };
let component: Record<string, unknown> | null = null;

afterEach(() => { if (component) unmount(component); component = null; });

async function showDiagram() {
	openTabDocumentImage(HOST, TAB, DRAWN);
	const comments = {
		artifactId: 'art', projectId: null, readOnly: false, post: vi.fn(),
		refreshDiagram: vi.fn(async () => ({ ...DRAWN, src: 'blob:new', diagramSource: 'new' })),
	};
	const host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ArtifactDocumentImageOverlay, {
		target: host,
		props: { image: tabDocumentImage(HOST, TAB)!, comments, target: TARGET, tabThreadId: HOST, onBack: () => {}, onPlace: () => {}, onReplyJump: () => {} },
	});
	for (let i = 0; i < 5; i++) { await Promise.resolve(); flushSync(); }
	return comments;
}

describe('an open diagram refreshed from its overlay', () => {
	it('replaces the snapshot held by the leaf’s tab, which is not the document’s thread when the leaf shows another thread’s document', async () => {
		const comments = await showDiagram();
		expect(comments.refreshDiagram).toHaveBeenCalledOnce();
		expect(tabDocumentImage(HOST, TAB)).toMatchObject({ src: 'blob:new', diagramSource: 'new', from: 9, to: 40 });
	});
});
