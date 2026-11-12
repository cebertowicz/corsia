import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { visibleRegion } from '$lib/cm/visible-region';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({ docComments: { subscribe: (run: (value: object) => void) => { run({}); return () => {}; } }, loadComments: vi.fn() }));

import { MARGIN_MIN } from '$lib/docCommentSurfaceState.svelte';
import DocCommentSurface from './DocCommentSurface.svelte';

let host: HTMLElement;
let bounds: HTMLElement;
let component: Record<string, unknown> | null = null;
const target = { instanceId: 'tab-1', path: 'notes.md', name: 'Notes', projectId: 'project-1', threadId: 'thread-1', artifactId: 'artifact-1' };
const SHEET_HEIGHT = 300;
const SCREEN = 700;

/** The editor of a document surface: its scroller shows the whole phone screen. */
function editorIn(surface: HTMLElement) {
	const view = new EditorView({ state: EditorState.create({ doc: 'text' }), parent: surface });
	view.scrollDOM.style.overflowY = 'auto';
	view.scrollDOM.getBoundingClientRect = () => new DOMRect(0, 0, 390, SCREEN);
	return view;
}

/** The new-comment sheet at phone width (or a popover beside it) over a document; jsdom has no layout, so the sheet is given one. */
function openSheet(state: { kind: 'draft' } | { kind: 'list' }, narrow = true, onRevealDraft = vi.fn(), canvasWidth = 0) {
	host = document.body.appendChild(document.createElement('div'));
	bounds = document.body.appendChild(document.createElement('div'));
	const view = editorIn(bounds);
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		if (this === bounds) return new DOMRect(0, 0, canvasWidth, SCREEN);
		return (this.hasAttribute('data-adaptive-drawer') ? new DOMRect(0, 0, 390, SCREEN) : new DOMRect()) as DOMRect;
	});
	vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
		return this.classList.contains('drawer-panel') ? SHEET_HEIGHT : 0;
	});
	component = mount(DocCommentSurface, {
		target: host,
		props: {
			state, anchor: () => new DOMRect(), bounds, narrow, comments: [], onRevealDraft,
			focusedId: null, draft: { text: 'Yes', label: '“Yes” · Delta, Status' }, onClose: vi.fn(), restoreFocus: vi.fn(),
			onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(), target, artifactId: 'artifact-1',
		},
	});
	flushSync();
	return { view, onRevealDraft };
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	bounds?.remove();
	document.body.replaceChildren();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('the phone composer sheet over a commented cell', () => {
	it('asks the editor to keep the draft in view once the sheet is up, and tells it when the keyboard moves it', async () => {
		const viewport = new EventTarget();
		vi.stubGlobal('visualViewport', viewport);
		const { onRevealDraft } = openSheet({ kind: 'draft' });
		expect(document.querySelector('.drawer-panel')).not.toBeNull();
		await vi.waitFor(() => expect(onRevealDraft).toHaveBeenCalledTimes(1));
		expect(onRevealDraft).toHaveBeenLastCalledWith(true);
		viewport.dispatchEvent(new Event('resize'));
		await vi.waitFor(() => expect(onRevealDraft).toHaveBeenCalledTimes(2));
		expect(onRevealDraft).toHaveBeenLastCalledWith(false);
	});

	it('does nothing where the composer is not a sheet', async () => {
		const { onRevealDraft } = openSheet({ kind: 'draft' }, false);
		expect(document.querySelector('.drawer-panel')).toBeNull();
		await new Promise((resolve) => setTimeout(resolve, 50));
		expect(onRevealDraft).not.toHaveBeenCalled();
	});

	it('keeps the text of its own editor above where the sheet rests, as long as the sheet is on screen', () => {
		const { view } = openSheet({ kind: 'draft' });
		const other = document.body.appendChild(document.createElement('div'));
		const elsewhere = editorIn(other);
		// The backdrop ends at the screen's bottom and the sheet is 300px tall: it rests at 544, wherever its intro has it.
		expect(visibleRegion(view).band).toEqual({ top: 0, bottom: SCREEN - SHEET_HEIGHT });
		expect(visibleRegion(elsewhere).band.bottom).toBe(SCREEN);
		unmount(component!);
		component = null;
		expect(visibleRegion(view).band.bottom).toBe(SCREEN);
	});

	it('does the same for the list sheet', () => {
		vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
		const { view } = openSheet({ kind: 'list' });
		expect(document.querySelector('[data-adaptive-drawer] [data-doc-comments-drawer]')).not.toBeNull();
		expect(visibleRegion(view).band.bottom).toBe(SCREEN - SHEET_HEIGHT);
	});

	// The inline form is the margin column, and it starts where the text beside it keeps 45 characters per line (660 px), not at 900 px.
	it('draws the list as a modal sheet on a canvas under MARGIN_MIN and as the margin column, with no scrim, from MARGIN_MIN', () => {
		vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
		// Each canvas is the column for its first pass, until it is measured; that holds the reader's row, which reads a text range's rects, and jsdom has none to give.
		Object.defineProperty(Range.prototype, 'getClientRects', { configurable: true, value: () => [] });
		Object.defineProperty(Range.prototype, 'getBoundingClientRect', { configurable: true, value: () => new DOMRect(0, 0, 0, 0) });
		openSheet({ kind: 'list' }, false, vi.fn(), MARGIN_MIN - 1);
		expect(document.querySelector('[data-adaptive-drawer]')?.classList.contains('nonmodal')).toBe(false);
		expect(document.querySelector('.drawer-panel')?.getAttribute('aria-modal')).toBe('true');
		unmount(component!);
		component = null;
		document.body.replaceChildren();
		openSheet({ kind: 'list' }, false, vi.fn(), MARGIN_MIN);
		expect(document.querySelector('[data-adaptive-drawer]')).toBeNull();
		expect(document.querySelector('[data-doc-comments-drawer]')).not.toBeNull();
		delete (Range.prototype as { getClientRects?: unknown }).getClientRects;
		delete (Range.prototype as { getBoundingClientRect?: unknown }).getBoundingClientRect;
	});
});
