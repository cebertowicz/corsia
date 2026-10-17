import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: vi.fn() }));

import DocCommentSurface from './DocCommentSurface.svelte';

let host: HTMLElement;
let bounds: HTMLElement;
let component: Record<string, unknown> | null = null;
const target = { instanceId: 'tab-1', path: 'notes.md', name: 'Notes', projectId: 'project-1', threadId: 'thread-1', artifactId: 'artifact-1' };
const SHEET_TOP = 400;

/** The new-comment sheet at phone width over a document whose commented cell is drawn at `subject`. */
function openDraftSheet(subject: DOMRect, narrow = true) {
	host = document.body.appendChild(document.createElement('div'));
	bounds = document.body.appendChild(document.createElement('div'));
	const scroller = bounds.appendChild(document.createElement('div'));
	scroller.className = 'cm-scroller';
	// jsdom has no layout: the sheet rests at SHEET_TOP, and the scroller reports where its content has scrolled to.
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return (this.classList.contains('drawer-panel') ? new DOMRect(0, SHEET_TOP, 390, 300) : new DOMRect()) as DOMRect;
	});
	component = mount(DocCommentSurface, {
		target: host,
		props: {
			state: { kind: 'draft' }, anchor: () => new DOMRect(0, subject.top - scroller.scrollTop, subject.width, subject.height), bounds, narrow, comments: [],
			focusedId: null, draft: { text: 'Yes', label: '“Yes” · Delta, Status' }, onClose: vi.fn(), restoreFocus: vi.fn(),
			onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(), target, artifactId: 'artifact-1',
		},
	});
	flushSync();
	return scroller;
}
const settled = () => new Promise((resolve) => setTimeout(resolve, 0));

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	bounds?.remove();
	vi.restoreAllMocks();
});

describe('the phone composer sheet over a commented cell', () => {
	it('scrolls the editor so the cell clears the sheet', async () => {
		// The cell is drawn behind the sheet (its bottom is 120px below the sheet's top edge).
		const scroller = openDraftSheet(new DOMRect(0, SHEET_TOP - 20, 390, 140));
		expect(document.querySelector('.drawer-panel')).not.toBeNull();
		await settled();
		expect(scroller.scrollTop).toBe(120 + 8);
	});

	it('leaves the editor where it is when the cell is already above the sheet', async () => {
		const scroller = openDraftSheet(new DOMRect(0, 100, 390, 40));
		await settled();
		expect(scroller.scrollTop).toBe(0);
	});

	it('does nothing where the composer is not a sheet', async () => {
		const scroller = openDraftSheet(new DOMRect(0, SHEET_TOP - 20, 390, 140), false);
		expect(document.querySelector('.drawer-panel')).toBeNull();
		await settled();
		expect(scroller.scrollTop).toBe(0);
	});
});
