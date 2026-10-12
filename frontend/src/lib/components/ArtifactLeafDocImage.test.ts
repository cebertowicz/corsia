import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn() }));
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
	returnDocFeedback: vi.fn(async () => ({ undelivered_batches: [] })),
	leaveDocFeedback: vi.fn(async () => ({ undelivered_batches: [] })),
}));
vi.mock('$lib/stores/liveDocuments', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/liveDocuments')>()),
	openLiveDocument: vi.fn(async () => null),
}));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({
	...(await importOriginal<object>()),
	fetchReviewBar: vi.fn(async () => ({ ask_raised: false })),
}));

import ArtifactLeaf from './ArtifactLeaf.svelte';
import { backLayerDepth, resetBackLayers } from '$lib/historyBackLayer';
import { threadWorkspaces, openArtifactTab } from '$lib/stores/artifactWorkspace';
import { closeDocumentImage, documentImageViewers, openDocumentImage, remapDocumentImage } from '$lib/stores/documentImageViewer';
import { paneCollapsed } from '$lib/stores/paneCollapsed';
import { paneOpen } from '$lib/stores/paneOpen';
import { token } from '$lib/stores/auth';
import { ImageFileError } from '$lib/docOpenRefusal';

const THREAD = 'thread-doc-image';
const OTHER = 'thread-other';
const DOC = { text: '# Notes\n\n![Mock screen](shots/a.png)\n', revision: 'r-doc', writable: true, artifact: null, freshness: null };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let realRO: typeof ResizeObserver;
let leafWidth = 420;
const realWidth = window.innerWidth;

const settle = async () => { for (let i = 0; i < 5; i++) { await Promise.resolve(); flushSync(); } };
const setWindowWidth = (width: number) => Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
const activeInstance = () => {
	const workspace = get(threadWorkspaces)[THREAD];
	return workspace.tabs.find((t) => t.instanceId === workspace.activeTabId)!.instanceId;
};
const image = (instanceId: string) => ({ instanceId, src: '/api/files/content?path=%2Fws%2Fshots%2Fa.png&raw=true', alt: 'Mock screen', from: 9, to: 36, scrollTop: 120, top: 0 });

async function mountLeaf(mode: 'expanded' | 'rail' | 'compact' = 'expanded', props: Record<string, unknown> = {}) {
	component = mount(ArtifactLeaf, { target: host, props: { threadId: THREAD, mode, ...props } });
	await settle();
}

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
	localStorage.clear();
	threadWorkspaces.set({});
	documentImageViewers.set({});
	paneCollapsed.set(false);
	paneOpen.set(false);
	resetBackLayers();
	vi.spyOn(history, 'pushState').mockImplementation(() => {});
	vi.spyOn(history, 'back').mockImplementation(() => {});
	setWindowWidth(1440);
	leafWidth = 420;
	token.set('test-bearer');
	mocks.fetchContent.mockReset().mockImplementation(async (path: string) => {
		if (/\.png$/i.test(path)) throw new ImageFileError();
		return DOC;
	});
	mocks.fetchRevision.mockReset().mockResolvedValue({
		exists: true, path: null, revision: 'r-image', media_type: 'image/png', writable: false, artifact_id: 'art-img',
	});
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		const width = this.classList.contains('leaf') ? leafWidth : 0;
		return { x: 0, y: 0, left: 0, top: 0, right: width, bottom: 0, width, height: 0, toJSON() {} } as DOMRect;
	});
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.ResizeObserver = realRO;
	setWindowWidth(realWidth);
	resetBackLayers();
	vi.restoreAllMocks();
});

describe('an image of a document opened in the canvas', () => {
	beforeEach(() => {
		openArtifactTab(THREAD, { artifactId: 'art-doc', path: '/ws/notes.md', title: 'notes.md' });
	});

	it('draws the viewer over the document, which stays mounted, hidden and inert', async () => {
		await mountLeaf();
		expect(host.querySelector('.doc')).not.toBeNull();
		expect(host.querySelector('.image-overlay')).toBeNull();

		openDocumentImage(THREAD, image(activeInstance()));
		await settle();

		const overlay = host.querySelector('.image-overlay')!;
		expect(overlay.querySelector('img')!.alt).toBe('Mock screen');
		expect(overlay.querySelector('button[aria-label="Back to notes.md"]')).not.toBeNull();
		const docHost = host.querySelector<HTMLElement>('.doc-host')!;
		expect((docHost as HTMLElement & { inert: boolean }).inert).toBe(true);
		expect(docHost.classList.contains('covered')).toBe(true);
		expect(docHost.querySelector('.doc')).not.toBeNull();
		// A document's review bar is the document's: none shows over its image.
		expect(overlay.querySelector('.review-bar')).toBeNull();
		expect(host.querySelectorAll('.tab').length).toBe(1);
	});

	it('goes back to the same document from its Back button and from Escape', async () => {
		await mountLeaf();
		const closed = () => host.querySelector('.image-overlay') === null && !(host.querySelector('.doc-host') as HTMLElement & { inert: boolean }).inert;

		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(closed()).toBe(true);

		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		await settle();
		expect(closed()).toBe(true);
		expect(host.querySelector('.doc')).not.toBeNull();
	});

	it('goes back to the document on the phone or browser Back, before leaving the document', async () => {
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		expect(backLayerDepth()).toBe(1);
		window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		expect(backLayerDepth()).toBe(0);
		expect(host.querySelector('.doc')).not.toBeNull();
	});

	it('does not let the document answer Escape while the viewer covers it', async () => {
		const leave = vi.fn();
		await mountLeaf('compact', { escapeCloses: true, onEscapeClose: leave });
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		await settle();
		expect(leave).not.toHaveBeenCalled();
		expect(host.querySelector('.image-overlay')).toBeNull();

		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		expect(leave).toHaveBeenCalledTimes(1);
	});

	it('closes when the user switches to another tab', async () => {
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		openArtifactTab(THREAD, { artifactId: 'art-two', path: '/ws/two.md', title: 'two.md' });
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		expect(get(documentImageViewers)[THREAD]).toBeUndefined();
	});

	it('closes when its tab is closed, when the leaf collapses to the rail and when the leaf goes away', async () => {
		await mountLeaf();
		const instanceId = activeInstance();
		openDocumentImage(THREAD, image(instanceId));
		await settle();
		unmount(component!);
		component = null;
		expect(get(documentImageViewers)[THREAD]).toBeUndefined();

		openDocumentImage(THREAD, image(instanceId));
		await mountLeaf('rail');
		expect(get(documentImageViewers)[THREAD]).toBeUndefined();
		expect(host.querySelector('.image-overlay')).toBeNull();
	});

	it('closes when the thread changes, leaving the other thread\'s viewer alone', async () => {
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		openDocumentImage(OTHER, image('elsewhere'));
		await settle();
		unmount(component!);
		component = null;
		expect(Object.keys(get(documentImageViewers))).toEqual([OTHER]);
		closeDocumentImage(OTHER);
	});

	it('collapses the workspace pane for the viewer and brings it back when the viewer closes', async () => {
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		expect(get(paneCollapsed)).toBe(true);

		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(get(paneCollapsed)).toBe(false);
	});

	it('brings the pane back on every other way out too', async () => {
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		expect(get(paneCollapsed)).toBe(true);
		openArtifactTab(THREAD, { artifactId: 'art-two', path: '/ws/two.md', title: 'two.md' });
		await settle();
		expect(get(paneCollapsed)).toBe(false);

		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		expect(get(paneCollapsed)).toBe(true);
		unmount(component!);
		component = null;
		expect(get(paneCollapsed)).toBe(false);
	});

	it('leaves the pane alone for a leaf already wide, at drawer widths, and when it is already collapsed', async () => {
		leafWidth = 640;
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		expect(get(paneCollapsed)).toBe(false);
		unmount(component!);
		component = null;

		leafWidth = 420;
		setWindowWidth(900);
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		expect(get(paneCollapsed)).toBe(false);
		unmount(component!);
		component = null;

		setWindowWidth(1440);
		paneCollapsed.set(true);
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(get(paneCollapsed)).toBe(true);
	});

	it('offers comments on the image when the document has them, posting where its markdown is now', async () => {
		await mountLeaf();
		const post = vi.fn(async () => 'new');
		openDocumentImage(THREAD, { ...image(activeInstance()), comments: { artifactId: 'art-doc', projectId: null, readOnly: false, post } });
		await settle();
		const overlay = host.querySelector('.image-overlay')!;
		expect(overlay.querySelector('[data-image-comment]')).not.toBeNull();
		expect(overlay.querySelector('[data-image-comments-pill]')).not.toBeNull();
		expect(overlay.querySelector('button[aria-label="Back to notes.md"]')).not.toBeNull();

		remapDocumentImage(THREAD, activeInstance(), (pos) => pos + 3);
		await settle();
		const img = overlay.querySelector<HTMLImageElement>('.pad img')!;
		Object.defineProperty(img, 'naturalWidth', { configurable: true, value: 1000 });
		Object.defineProperty(img, 'naturalHeight', { configurable: true, value: 500 });
		img.dispatchEvent(new Event('load'));
		await settle();
		overlay.querySelector<HTMLButtonElement>('[data-image-comment]')!.click();
		await settle();
		overlay.querySelector<HTMLElement>('[data-image-regions] .surface')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
		await settle();
		const textarea = host.querySelector<HTMLTextAreaElement>('.image-overlay textarea')!;
		textarea.value = 'Too dark';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		host.querySelector<HTMLButtonElement>('.image-overlay button.submit')!.click();
		await settle();
		expect(post).toHaveBeenCalledWith(12, 39, { x: 0, y: 0, w: 1, h: 1 }, 'Too dark');
	});

	it('keeps the pane as the user left it when they toggle it while the viewer is open', async () => {
		await mountLeaf();
		openDocumentImage(THREAD, image(activeInstance()));
		await settle();
		paneCollapsed.set(false);
		paneCollapsed.set(true);
		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(get(paneCollapsed)).toBe(true);
	});
});

describe('an image artifact', () => {
	it('opens in the same viewer with no Back, no pane change, and its own review bar', async () => {
		openArtifactTab(THREAD, { artifactId: 'art-img', path: '/ws/shot.png', title: 'shot.png' });
		await mountLeaf();
		expect(host.querySelector('.image-viewer img')).not.toBeNull();
		expect(host.querySelector('.image-viewer .review-bar, .image-viewer .bar')).not.toBeNull();
		expect(host.querySelector('.image-viewer button.back')).toBeNull();
		expect(host.querySelector('.image-overlay')).toBeNull();
		expect(get(paneCollapsed)).toBe(false);
		expect(backLayerDepth()).toBe(0);
		const labels = [...host.querySelectorAll('.image-viewer .zoom button')].map((b) => b.getAttribute('aria-label') ?? b.textContent?.trim());
		expect(labels).toEqual(expect.arrayContaining(['Fit', '100%', 'Zoom out', 'Zoom in']));
	});
});
