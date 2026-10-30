import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), addDocImageComment: vi.fn() }));
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
	sendDocFeedback: vi.fn(async () => ({ woken: [], undelivered_batches: [], open_count: 0, document_held_count: 0, image_held_count: 0 })),
}));
vi.mock('$lib/stores/liveDocuments', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/liveDocuments')>()),
	openLiveDocument: vi.fn(async () => null),
}));
vi.mock('$lib/stores/imageRegionComments', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/imageRegionComments')>()),
	addDocImageComment: mocks.addDocImageComment,
}));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({
	...(await importOriginal<object>()),
	fetchReviewBar: vi.fn(async () => ({ ask_raised: false })),
}));

import ArtifactLeaf from './ArtifactLeaf.svelte';
import ArtifactLeafModeTestHost from './ArtifactLeafModeTestHost.test.svelte';
import { backLayerDepth, resetBackLayers } from '$lib/historyBackLayer';
import { threadWorkspaces, openArtifactTab } from '$lib/stores/artifactWorkspace';
import {
	clearTabDocumentImage, openTabDocumentImage, remapTabDocumentImage, tabDocumentImage, tabViewState,
} from '$lib/stores/tabViewState';
import { EditorView } from '@codemirror/view';
import { forceParsing } from '@codemirror/language';
import { paneCollapsed, paneSnapping } from '$lib/stores/paneCollapsed';
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
const image = () => ({ src: '/api/files/content?path=%2Fws%2Fshots%2Fa.png&raw=true', alt: 'Mock screen', from: 9, to: 36, scrollTop: 120, top: 0 });
const showImage = (instanceId = activeInstance()) => openTabDocumentImage(THREAD, instanceId, image());
const notesTab = () => [...host.querySelectorAll<HTMLButtonElement>('.tab')].find((tab) => tab.textContent?.includes('notes.md'))!;

/** Draws a region over the whole loaded image and posts a comment on it from the open viewer. */
async function commentOnWholeImage(text: string) {
	const overlay = host.querySelector('.image-overlay')!;
	await loadOverlayImage();
	overlay.querySelector<HTMLButtonElement>('[data-image-comment]')!.click();
	await settle();
	overlay.querySelector<HTMLElement>('[data-image-regions] .surface')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
	await settle();
	const textarea = document.body.querySelector<HTMLTextAreaElement>('textarea')!;
	textarea.value = text;
	textarea.dispatchEvent(new Event('input', { bubbles: true }));
	await settle();
	document.body.querySelector<HTMLButtonElement>('button.submit')!.click();
	await settle();
}

async function mountLeaf(mode: 'expanded' | 'rail' | 'compact' = 'expanded', props: Record<string, unknown> = {}) {
	component = mount(ArtifactLeaf, { target: host, props: { threadId: THREAD, mode, ...props } });
	await settle();
}

async function openImageFromDocument() {
	// The first parse stops after 20ms of wall time and finishes on a timer these tests never run: under load the image would not
	// be drawn yet.
	const editor = EditorView.findFromDOM(host.querySelector<HTMLElement>('.doc-host .cm-content')!)!;
	if (!forceParsing(editor, editor.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	await settle();
	const image = host.querySelector<HTMLImageElement>('.cm-md-image img')!;
	Object.defineProperties(image, {
		complete: { configurable: true, value: true },
		naturalWidth: { configurable: true, value: 1000 },
		naturalHeight: { configurable: true, value: 500 },
	});
	image.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, cancelable: true }));
	image.dispatchEvent(new MouseEvent('click', { bubbles: true, button: 0, cancelable: true }));
	await settle();
	return host.querySelector<HTMLElement>('.image-overlay')!;
}

async function loadOverlayImage() {
	const image = host.querySelector<HTMLImageElement>('.image-overlay .pad img')!;
	Object.defineProperties(image, {
		naturalWidth: { configurable: true, value: 1000 },
		naturalHeight: { configurable: true, value: 500 },
	});
	image.dispatchEvent(new Event('load'));
	await settle();
}

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
	localStorage.clear();
	threadWorkspaces.set({});
	mocks.addDocImageComment.mockReset().mockResolvedValue('new');
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

		showImage();
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

	it('opens through ArtifactLeaf, restores its camera after a tab switch, then returns to the document place', async () => {
		await mountLeaf();
		const documentScroller = host.querySelector<HTMLElement>('.cm-scroller')!;
		documentScroller.scrollTop = 160;
		const overlay = await openImageFromDocument();
		expect(overlay).not.toBeNull();
		await loadOverlayImage();
		[...overlay.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent?.trim() === '100%')!.click();
		const viewport = overlay.querySelector<HTMLElement>('.viewport')!;
		viewport.scrollLeft = 111;
		viewport.scrollTop = 73;
		viewport.dispatchEvent(new Event('scroll'));
		await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
		const original = activeInstance();
		expect(tabViewState(THREAD, original)?.documentImage?.camera).toEqual({ scrollLeft: 111, scrollTop: 73, scale: 1 });

		openArtifactTab(THREAD, { artifactId: 'art-two', path: '/ws/two.md', title: 'two.md' });
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		notesTab().click();
		await settle();
		expect(host.querySelector('.image-overlay')).not.toBeNull();
		await loadOverlayImage();
		expect(host.querySelector('.image-overlay .level')?.textContent).toBe('100%');
		const restoredViewport = host.querySelector<HTMLElement>('.image-overlay .viewport')!;
		expect([restoredViewport.scrollLeft, restoredViewport.scrollTop]).toEqual([111, 73]);

		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		expect(documentScroller.scrollTop).toBe(160);
		expect(tabViewState(THREAD, original)?.documentImage).toBeUndefined();
	});

	it('clears an image opened through ArtifactLeaf when the pane collapses or its tab closes', async () => {
		component = mount(ArtifactLeafModeTestHost, { target: host });
		await settle();
		await openImageFromDocument();
		const modeHost = component as unknown as { setMode: (mode: 'expanded' | 'rail') => void };
		modeHost.setMode('rail');
		await settle();
		modeHost.setMode('expanded');
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		expect(tabViewState(THREAD, activeInstance())?.documentImage).toBeUndefined();

		await openImageFromDocument();
		const closing = activeInstance();
		host.querySelector<HTMLButtonElement>('[aria-label="Close notes.md"]')!.click();
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		expect(tabViewState(THREAD, closing)).toBeNull();
	});

	it('goes back to the same document from its Back button and from Escape', async () => {
		await mountLeaf();
		const closed = () => host.querySelector('.image-overlay') === null && !(host.querySelector('.doc-host') as HTMLElement & { inert: boolean }).inert;

		showImage();
		await settle();
		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(closed()).toBe(true);

		showImage();
		await settle();
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		await settle();
		expect(closed()).toBe(true);
		expect(host.querySelector('.doc')).not.toBeNull();
	});

	it('goes back to the document on the phone or browser Back, before leaving the document', async () => {
		await mountLeaf();
		showImage();
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
		showImage();
		await settle();
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		await settle();
		expect(leave).not.toHaveBeenCalled();
		expect(host.querySelector('.image-overlay')).toBeNull();

		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		expect(leave).toHaveBeenCalledTimes(1);
	});

	it('hides with its tab when the user switches to another, and shows again when they come back', async () => {
		await mountLeaf();
		const original = activeInstance();
		showImage();
		await settle();
		openArtifactTab(THREAD, { artifactId: 'art-two', path: '/ws/two.md', title: 'two.md' });
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		expect(tabDocumentImage(THREAD, original)).not.toBeNull();
		notesTab().click();
		await settle();
		expect(host.querySelector('.image-overlay img')?.getAttribute('alt')).toBe('Mock screen');
	});

	it('stays with its tab when the leaf goes away, and closes when the leaf collapses to the rail', async () => {
		await mountLeaf();
		const instanceId = activeInstance();
		showImage(instanceId);
		await settle();
		unmount(component!);
		component = null;
		expect(tabDocumentImage(THREAD, instanceId)).not.toBeNull();
		await mountLeaf();
		expect(host.querySelector('.image-overlay')).not.toBeNull();
		unmount(component!);
		component = null;

		await mountLeaf('rail');
		expect(tabDocumentImage(THREAD, instanceId)).toBeNull();
		expect(host.querySelector('.image-overlay')).toBeNull();
	});

	it('keeps each thread\'s image with its own tab', async () => {
		await mountLeaf();
		const mine = activeInstance();
		showImage(mine);
		openTabDocumentImage(OTHER, 'elsewhere', image());
		await settle();
		unmount(component!);
		component = null;
		expect(tabDocumentImage(THREAD, mine)).not.toBeNull();
		expect(tabDocumentImage(OTHER, 'elsewhere')).not.toBeNull();
		clearTabDocumentImage(OTHER, 'elsewhere');
	});

	it('collapses the workspace pane for the viewer and brings it back when the viewer closes', async () => {
		await mountLeaf();
		showImage();
		await settle();
		expect(get(paneCollapsed)).toBe(true);

		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(get(paneCollapsed)).toBe(false);
	});

	it('brings the pane back on every other way out too', async () => {
		await mountLeaf();
		showImage();
		await settle();
		expect(get(paneCollapsed)).toBe(true);
		openArtifactTab(THREAD, { artifactId: 'art-two', path: '/ws/two.md', title: 'two.md' });
		await settle();
		expect(get(paneCollapsed)).toBe(false);

		showImage();
		await settle();
		expect(get(paneCollapsed)).toBe(true);
		unmount(component!);
		component = null;
		expect(get(paneCollapsed)).toBe(false);
	});

	it('eases the pane for opening an image and for Back, and snaps it for a tab switch and a return', async () => {
		await mountLeaf();
		const changes: string[] = [];
		const stop = paneCollapsed.subscribe((collapsed) => changes.push(`${collapsed ? 'collapse' : 'expand'} ${get(paneSnapping) ? 'snap' : 'ease'}`));
		changes.length = 0;

		await openImageFromDocument();
		openArtifactTab(THREAD, { artifactId: 'art-two', path: '/ws/two.md', title: 'two.md' });
		await settle();
		notesTab().click();
		await settle();
		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		stop();

		expect(changes).toEqual(['collapse ease', 'expand snap', 'collapse snap', 'expand ease']);
	});

	it('leaves the pane alone for a leaf already wide, at drawer widths, and when it is already collapsed', async () => {
		leafWidth = 640;
		await mountLeaf();
		showImage();
		await settle();
		expect(get(paneCollapsed)).toBe(false);
		unmount(component!);
		component = null;

		leafWidth = 420;
		setWindowWidth(900);
		await mountLeaf();
		showImage();
		await settle();
		expect(get(paneCollapsed)).toBe(false);
		unmount(component!);
		component = null;

		setWindowWidth(1440);
		paneCollapsed.set(true);
		await mountLeaf();
		showImage();
		await settle();
		host.querySelector<HTMLButtonElement>('.image-overlay button.back')!.click();
		await settle();
		expect(get(paneCollapsed)).toBe(true);
	});

	it('offers comments on the image from the document under it, posting where its markdown is now', async () => {
		await mountLeaf();
		showImage();
		await settle();
		const overlay = host.querySelector('.image-overlay')!;
		expect(overlay.querySelector('[data-image-comment]')).not.toBeNull();
		expect(overlay.querySelector('[data-image-comments-pill]')).not.toBeNull();
		expect(overlay.querySelector('button[aria-label="Back to notes.md"]')).not.toBeNull();

		remapTabDocumentImage(THREAD, activeInstance(), (pos) => pos + 3);
		await settle();
		await commentOnWholeImage('Too dark');
		expect(mocks.addDocImageComment).toHaveBeenCalledWith(
			expect.objectContaining({ artifactId: 'art-doc', from: 12, to: 39 }), { x: 0, y: 0, w: 1, h: 1 }, 'Too dark',
		);
	});

	it('keeps an open image on its markdown through an edit of the document, across a tab switch and back', async () => {
		await mountLeaf();
		const original = activeInstance();
		await openImageFromDocument();
		expect(tabDocumentImage(THREAD, original)).toMatchObject({ from: 9, to: 36 });

		EditorView.findFromDOM(host.querySelector<HTMLElement>('.doc-host .cm-content')!)!.dispatch({ changes: { from: 0, insert: 'Intro\n' } });
		await settle();
		expect(tabDocumentImage(THREAD, original)).toMatchObject({ from: 15, to: 42 });

		openArtifactTab(THREAD, { artifactId: 'art-two', path: '/ws/two.md', title: 'two.md' });
		await settle();
		expect(host.querySelector('.image-overlay')).toBeNull();
		notesTab().click();
		await settle();
		expect(tabDocumentImage(THREAD, original)).toMatchObject({ from: 15, to: 42 });
		await commentOnWholeImage('Moved');
		expect(mocks.addDocImageComment).toHaveBeenCalledWith(
			expect.objectContaining({ artifactId: 'art-doc', from: 15, to: 42 }), { x: 0, y: 0, w: 1, h: 1 }, 'Moved',
		);
	});

	it('keeps the pane as the user left it when they toggle it while the viewer is open', async () => {
		await mountLeaf();
		showImage();
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
