import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { visibleRegion } from '$lib/cm/visible-region';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: vi.fn() }));

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
function openSheet(state: { kind: 'draft' } | { kind: 'list' }, narrow = true, onRevealDraft = vi.fn()) {
	host = document.body.appendChild(document.createElement('div'));
	bounds = document.body.appendChild(document.createElement('div'));
	const view = editorIn(bounds);
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
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
	it('asks the editor to keep the draft in view once the sheet is up, and again when the keyboard moves it', async () => {
		const viewport = new EventTarget();
		vi.stubGlobal('visualViewport', viewport);
		const { onRevealDraft } = openSheet({ kind: 'draft' });
		expect(document.querySelector('.drawer-panel')).not.toBeNull();
		await vi.waitFor(() => expect(onRevealDraft).toHaveBeenCalledTimes(1));
		viewport.dispatchEvent(new Event('resize'));
		await vi.waitFor(() => expect(onRevealDraft).toHaveBeenCalledTimes(2));
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
});
