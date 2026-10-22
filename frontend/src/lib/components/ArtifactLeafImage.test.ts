import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchReviewBar: vi.fn() }));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<object>()), fetchReviewBar: mocks.fetchReviewBar }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));

vi.mock('$lib/api/docs', () => ({
	fetchContent: mocks.fetchContent,
	fetchRevision: mocks.fetchRevision,
	fetchComments: vi.fn(async () => ({ comments: [], feedback: { owner_label: null, undelivered_batches: [] } })),
	writeContent: vi.fn(),
	registerArtifact: vi.fn(),
}));

vi.mock('$lib/api/docFeedback', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/docFeedback')>()),
}));
vi.mock('$lib/stores/liveDocuments', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/liveDocuments')>()),
	openLiveDocument: vi.fn(async () => null),
}));

import ArtifactLeaf from './ArtifactLeaf.svelte';
import { threadWorkspaces, openArtifactTab, applyTabRevisionCheck } from '$lib/stores/artifactWorkspace';
import { tabViewState } from '$lib/stores/tabViewState';
import { token } from '$lib/stores/auth';
import { ImageFileError } from '$lib/docOpenRefusal';

const THREAD = 'thread-image-leaf';
const DOC = { text: '# Notes', revision: 'r-doc', writable: true, artifact: null, freshness: null };
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
	// A tab always starts in the document viewer; the backend's refusal is what moves an image to the image one.
	mocks.fetchContent.mockReset().mockImplementation(async (path: string) => {
		if (/\.(png|gif)$/i.test(path)) throw new ImageFileError();
		return DOC;
	});
	mocks.fetchRevision.mockReset().mockResolvedValue({
		exists: true, path: null, revision: 'r-image', media_type: 'image/png', writable: false, artifact_id: 'art-1',
	});
	mocks.fetchReviewBar.mockReset().mockResolvedValue({
		ask_raised: true, hold_ids: ['h1'], ask_reason: 'Is this the right diagram?', ask_raised_at: '2026-10-10T10:00:00',
		asker: { id: 'a1', name: 'Docs agent' }, other_askers: [], other_asks: [], other_request_count: 0,
		round_feedback_count: 0, round_feedback_counts: { comments: 0, suggestions: 0, edits: 0 },
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
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/shot.PNG', title: 'shot.PNG' });
		await settle();

		const img = host.querySelector<HTMLImageElement>('.image-viewer img')!;
		expect(img).not.toBeNull();
		expect(img.alt).toBe('shot.PNG');
		const url = new URL(img.src);
		expect(url.pathname).toBe('/api/files/content');
		expect(url.searchParams.get('raw')).toBe('true');
		expect(url.searchParams.get('path')).toBe('/ws/shot.PNG');
		expect(host.querySelector('.doc')).toBeNull();
	});

	it('shows the review bar above an image with a raised ask, read for the image\'s own artifact', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/shot.png', title: 'shot.png' });
		await vi.waitFor(() => expect(host.querySelector('.image-viewer .review-bar')).toBeTruthy());

		expect(mocks.fetchReviewBar).toHaveBeenCalledWith('art-1');
		// The bar lives in its height-holding slot, which comes first: above the image.
		const first = host.querySelector('.image-viewer')!.firstElementChild!;
		expect(first.classList.contains('rb-slot')).toBe(true);
		expect(first.querySelector(':scope > .review-bar')).toBeTruthy();
		expect(host.textContent).toContain('Is this the right diagram?');
	});

	it('reports ready with the probed revision once the image has loaded', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/anim.gif', title: 'anim.gif' });
		await settle();
		host.querySelector('img')!.dispatchEvent(new Event('load'));
		await settle();

		const tab = get(threadWorkspaces)[THREAD].tabs[0];
		expect(tab.status).toBe('ready');
		expect(tab.loadedRevision).toBe('r-image');
	});

	it('shows the failure state when the image cannot be loaded', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/broken.png', title: 'broken.png' });
		await settle();
		host.querySelector('img')!.dispatchEvent(new Event('error'));
		await settle();

		expect(host.textContent).toContain("Couldn't load this image.");
		expect(host.querySelector('img')).toBeNull();
		expect(get(threadWorkspaces)[THREAD].tabs[0].status).toBe('error');
	});

	it('loads the image again from the failure state with Try again', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/broken.png', title: 'broken.png' });
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

	it('opens every tab in the document viewer first: the image viewer is only ever chosen by the backend', async () => {
		openArtifactTab(THREAD, { artifactId: null, path: '/ws/looks-like.png', title: 'looks-like.png' });
		mocks.fetchContent.mockResolvedValue(DOC);
		await settle();

		expect(mocks.fetchContent).toHaveBeenCalled();
		expect(host.querySelector('.image-viewer')).toBeNull();
	});

	it('gives a tab the backend calls an image the image glyph, and a text tab the document glyph whatever its name', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/shot.png', title: 'shot.png' });
		await settle();
		const glyph = (title: string) => [...host.querySelectorAll('.tab')].find((t) => t.textContent?.includes(title))!.querySelector('.tab-ico')!;
		expect(glyph('shot.png').querySelector('circle')).not.toBeNull();

		// Named like an image but opened as text: only the backend's answer picks the image glyph.
		mocks.fetchContent.mockResolvedValue(DOC);
		openArtifactTab(THREAD, { artifactId: 'art-2', path: '/ws/notes.md', title: 'notes.md' });
		await settle();
		expect(glyph('notes.md').querySelector('circle')).toBeNull();
		expect(glyph('shot.png').querySelector('circle')).not.toBeNull();
	});

	it('switches a tab to the image viewer when the text open is refused as an image, storing nothing on the tab', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/legacy.png', title: 'legacy.png' });
		await settle();

		expect(host.querySelector<HTMLImageElement>('.image-viewer img')?.alt).toBe('legacy.png');
		expect(host.textContent).not.toContain("Couldn't open this document.");
		expect(JSON.stringify(get(threadWorkspaces)[THREAD])).not.toMatch(/image|renderer/i);
	});

	it('shows a restored tab through the viewer its next load needs', async () => {
		threadWorkspaces.set({
			[THREAD]: {
				tabs: [{
					instanceId: 'i1', kind: 'document', artifactId: 'art-1', path: '/ws/shot.png', title: 'shot.png',
					loadedRevision: null, latestRevision: null, status: 'loading',
				}],
				activeTabId: 'i1', visible: true, activeSurface: 'artifact',
			} as never,
		});
		await settle();

		expect(host.querySelector('.image-viewer img')).not.toBeNull();
	});

	async function reloadFromStaleControl(revision: string) {
		const tab = get(threadWorkspaces)[THREAD].tabs[0];
		applyTabRevisionCheck(THREAD, tab.instanceId, {
			exists: true, path: tab.path, revision, writable: true, artifactId: 'art-1',
		});
		await settle();
		const control = host.querySelector<HTMLButtonElement>('.stale-control');
		expect(control).not.toBeNull();
		control!.click();
		await settle();
	}

	it('shows an image tab as text on its next probe once the file was replaced by text', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/swap.png', title: 'swap.png' });
		await settle();
		host.querySelector('img')!.dispatchEvent(new Event('load'));
		await settle();
		expect(host.querySelector('.image-viewer img')).not.toBeNull();

		mocks.fetchRevision.mockResolvedValue({
			exists: true, path: null, revision: 'r-text', media_type: null, writable: true, artifact_id: 'art-1',
		});
		mocks.fetchContent.mockReset().mockResolvedValue(DOC);
		await reloadFromStaleControl('r-text');

		expect(host.querySelector('.image-viewer')).toBeNull();
		expect(mocks.fetchContent).toHaveBeenCalledTimes(1);
	});

	it('shows a text tab as an image on its next load once the file was replaced by an image', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/swap.md', title: 'swap.md' });
		await settle();
		expect(host.querySelector('.image-viewer')).toBeNull();
		expect(mocks.fetchContent).toHaveBeenCalledTimes(1);

		mocks.fetchContent.mockReset().mockRejectedValue(new ImageFileError());
		await reloadFromStaleControl('r-new');

		expect(host.querySelector('.image-viewer img')).not.toBeNull();
	});

	it('keeps a non-image file in the document viewer', async () => {
		openArtifactTab(THREAD, { artifactId: null, path: '/ws/notes.md', title: 'notes.md' });
		await settle();

		expect(host.querySelector('.image-viewer')).toBeNull();
		expect(mocks.fetchContent).toHaveBeenCalled();
	});

	it('restores an image tab camera after remounting in this session', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-1', path: '/ws/camera.png', title: 'camera.png' });
		await settle();
		const first = host.querySelector<HTMLImageElement>('.image-viewer img')!;
		Object.defineProperties(first, {
			naturalWidth: { configurable: true, value: 1000 },
			naturalHeight: { configurable: true, value: 500 },
		});
		first.dispatchEvent(new Event('load'));
		await settle();
		const firstViewport = host.querySelector<HTMLElement>('.image-viewer .viewport')!;
		[...host.querySelectorAll<HTMLButtonElement>('.image-viewer button')].find((button) => button.textContent?.trim() === '100%')!.click();
		firstViewport.scrollLeft = 123;
		firstViewport.scrollTop = 77;
		firstViewport.dispatchEvent(new Event('scroll'));
		await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
		const instanceId = get(threadWorkspaces)[THREAD].tabs[0].instanceId;
		expect(tabViewState(THREAD, instanceId)?.imagePlace).toEqual({ scrollLeft: 123, scrollTop: 77, scale: 1 });

		unmount(component!);
		component = mount(ArtifactLeaf, { target: host, props: { threadId: THREAD, mode: 'expanded' } });
		await settle();
		const restored = host.querySelector<HTMLImageElement>('.image-viewer img')!;
		Object.defineProperties(restored, {
			naturalWidth: { configurable: true, value: 1000 },
			naturalHeight: { configurable: true, value: 500 },
		});
		restored.dispatchEvent(new Event('load'));
		await settle();

		expect(host.querySelector('.image-viewer .level')?.textContent).toBe('100%');
		const restoredViewport = host.querySelector<HTMLElement>('.image-viewer .viewport')!;
		expect([restoredViewport.scrollLeft, restoredViewport.scrollTop]).toEqual([123, 77]);
	});
});
