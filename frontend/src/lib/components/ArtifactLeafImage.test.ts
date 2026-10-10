import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn() }));

vi.mock('$lib/api/docs', () => ({
	fetchContent: mocks.fetchContent,
	fetchRevision: mocks.fetchRevision,
	writeContent: vi.fn(),
	registerArtifact: vi.fn(),
}));

import ArtifactLeaf from './ArtifactLeaf.svelte';
import { threadWorkspaces, openArtifactTab } from '$lib/stores/artifactWorkspace';
import { token } from '$lib/stores/auth';
import { ImageFileError } from '$lib/docOpenRefusal';
import { restoreWorkspace } from '$lib/artifactTabs';

const THREAD = 'thread-image-leaf';
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let realRO: typeof ResizeObserver;

async function settle() {
	for (let i = 0; i < 5; i++) {
		await Promise.resolve();
		flushSync();
	}
}

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	} as unknown as typeof ResizeObserver;
	localStorage.clear();
	threadWorkspaces.set({});
	token.set('test-bearer');
	mocks.fetchContent.mockReset().mockResolvedValue({ text: '# Notes', revision: 'r-doc', writable: true, artifact: null, freshness: null });
	mocks.fetchRevision.mockReset().mockResolvedValue({
		exists: true, path: null, revision: 'r-image', writable: false, artifact_id: 'art-1',
	});
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(ArtifactLeaf, { target: host, props: { threadId: THREAD, mode: 'expanded' } });
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.ResizeObserver = realRO;
});

describe('artifact leaf renderer choice', () => {
	it('opens an image path as a rendered image served from the raw file route', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/shot.PNG', title: 'shot.PNG', isImage: true });
		await settle();

		const img = host.querySelector<HTMLImageElement>('.image-viewer img')!;
		expect(img).not.toBeNull();
		expect(img.alt).toBe('shot.PNG');
		const url = new URL(img.src);
		expect(url.pathname).toBe('/api/files/content');
		expect(url.searchParams.get('raw')).toBe('true');
		expect(url.searchParams.get('path')).toBe('/ws/shot.PNG');
		expect(mocks.fetchContent).not.toHaveBeenCalled();
		expect(host.querySelector('.doc')).toBeNull();
	});

	it('reports ready with the probed revision once the image has loaded', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/anim.gif', title: 'anim.gif', isImage: true });
		await settle();
		host.querySelector('img')!.dispatchEvent(new Event('load'));
		await settle();

		const tab = get(threadWorkspaces)[THREAD].tabs[0];
		expect(tab.status).toBe('ready');
		expect(tab.loadedRevision).toBe('r-image');
	});

	it('shows the failure state when the image cannot be loaded', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/broken.png', title: 'broken.png', isImage: true });
		await settle();
		host.querySelector('img')!.dispatchEvent(new Event('error'));
		await settle();

		expect(host.textContent).toContain("Couldn't load this image.");
		expect(host.querySelector('img')).toBeNull();
		expect(get(threadWorkspaces)[THREAD].tabs[0].status).toBe('error');
	});

	it('loads the image again from the failure state with Try again', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/broken.png', title: 'broken.png', isImage: true });
		await settle();
		host.querySelector('img')!.dispatchEvent(new Event('error'));
		await settle();

		const retry = [...host.querySelectorAll<HTMLButtonElement>('.load-error button')].find(
			(b) => b.textContent === 'Try again'
		)!;
		expect(retry).toBeDefined();
		retry.click();
		await settle();
		expect(mocks.fetchRevision).toHaveBeenCalledTimes(2);
		host.querySelector('img')!.dispatchEvent(new Event('load'));
		await settle();

		expect(host.textContent).not.toContain("Couldn't load this image.");
		expect(get(threadWorkspaces)[THREAD].tabs[0].status).toBe('ready');
	});

	it('does not take a path for an image: without the backend fact the tab opens as a document', async () => {
		openArtifactTab(THREAD, { artifactId: null, path: '/ws/looks-like.png', title: 'looks-like.png' });
		await settle();

		expect(host.querySelector('.image-viewer')).toBeNull();
		expect(mocks.fetchContent).toHaveBeenCalled();
	});

	it('switches a tab to the image viewer when the text open is refused as an image', async () => {
		mocks.fetchContent.mockRejectedValue(new ImageFileError());
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/legacy.png', title: 'legacy.png' });
		await settle();

		expect(get(threadWorkspaces)[THREAD].tabs[0].isImage).toBe(true);
		expect(host.querySelector<HTMLImageElement>('.image-viewer img')?.alt).toBe('legacy.png');
		expect(host.textContent).not.toContain("Couldn't open this document.");
	});

	it('shows a restored image tab as an image without asking the document route', async () => {
		const restored = restoreWorkspace({
			tabs: [{ instanceId: 'i1', artifactId: 'art-1', path: '/ws/shot.png', title: 'shot.png', isImage: true }],
			activeTabId: 'i1', visible: true, activeSurface: 'artifact',
		});
		threadWorkspaces.set({ [THREAD]: restored! });
		await settle();

		expect(host.querySelector('.image-viewer img')).not.toBeNull();
		expect(mocks.fetchContent).not.toHaveBeenCalled();
	});

	it('keeps a non-image file in the document viewer', async () => {
		openArtifactTab(THREAD, { artifactId: null, path: '/ws/notes.md', title: 'notes.md' });
		await settle();

		expect(host.querySelector('.image-viewer')).toBeNull();
		expect(mocks.fetchContent).toHaveBeenCalled();
	});
});
