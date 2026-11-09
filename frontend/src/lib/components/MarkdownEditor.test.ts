import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import { commentRanges } from '$lib/cm/comment-marks';

vi.mock('$lib/api/docs', () => ({ fetchComments: vi.fn(), postComment: vi.fn(), patchComment: vi.fn(), persistPositions: vi.fn() }));

import MarkdownEditor from './MarkdownEditor.svelte';
import { docComments, remapComments } from '$lib/stores/docComments';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	docComments.set({});
});

describe('MarkdownEditor edits', () => {
	it("reports the user's typing as an edit but not a text sync", () => {
		docComments.set({ artifact: [{
			id: 'comment', from: 4, to: 9, anchor: 'quick', author: 'agent-1', text: 'note', at: 'now', resolved: false, replies: [],
		}] });
		const text = writable('The quick brown fox.');
		const live = fromStore(text);
		// As DocViewer wires it: an edit's position map goes to the comment store.
		const onEdit = vi.fn((mapPos: (position: number) => number) => remapComments('artifact', mapPos));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(MarkdownEditor, {
			target: host,
			props: Object.defineProperties({ onEdit }, Object.getOwnPropertyDescriptors({
				get value() { return live.current; },
				set value(next: string) { text.set(next); },
			})),
		});
		flushSync();
		const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;

		// The viewer replaces the text: a reload after the file changed, or an accepted suggestion.
		text.set('The slow brown fox.'); flushSync();
		expect(view.state.doc.toString()).toBe('The slow brown fox.');
		expect(onEdit).not.toHaveBeenCalled();
		expect(get(docComments).artifact[0]).toMatchObject({ from: 4, to: 9 });

		// Typing before the quote moves it.
		view.dispatch({ changes: { from: 0, insert: 'So. ' }, userEvent: 'input.type' }); flushSync();
		expect(get(text)).toBe('So. The slow brown fox.');
		expect(onEdit).toHaveBeenCalledOnce();
		expect(get(docComments).artifact[0]).toMatchObject({ from: 8, to: 13 });
	});

	it('keeps a comment mark on text a replaced value leaves alone', () => {
		const text = writable('The quick brown fox. Then more.');
		const live = fromStore(text);
		const marks = writable([{ from: 4, to: 9, id: 'kept' }]);
		const liveMarks = fromStore(marks);
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(MarkdownEditor, {
			target: host,
			props: Object.defineProperties({}, Object.getOwnPropertyDescriptors({
				get value() { return live.current; },
				set value(next: string) { text.set(next); },
				get highlights() { return liveMarks.current; },
			})),
		});
		flushSync();
		const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
		expect(view.state.field(commentRanges)).toHaveLength(1);

		// A restore writes an older text: only the span that differs is replaced.
		text.set('The quick brown fox. Else more.'); flushSync();
		expect(view.state.doc.toString()).toBe('The quick brown fox. Else more.');
		expect(view.state.field(commentRanges)).toMatchObject([{ id: 'kept', from: 4, to: 9 }]);
	});

	it('keeps a comment mark on its words when a reloaded file gains a line before it', () => {
		const text = writable('The quick brown fox. Then more.');
		const live = fromStore(text);
		const marks = writable([{ from: 4, to: 9, id: 'kept' }]);
		const liveMarks = fromStore(marks);
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(MarkdownEditor, {
			target: host,
			props: Object.defineProperties({}, Object.getOwnPropertyDescriptors({
				get value() { return live.current; },
				set value(next: string) { text.set(next); },
				get highlights() { return liveMarks.current; },
			})),
		});
		flushSync();
		const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;

		// The file changed on disk (the stale-file Reload path, not Restore): one new span before the mark.
		text.set('Intro. The quick brown fox. Then more.'); flushSync();
		expect(view.state.doc.toString()).toBe('Intro. The quick brown fox. Then more.');
		expect(view.state.field(commentRanges)).toMatchObject([{ id: 'kept', from: 11, to: 16 }]);
		expect(view.state.sliceDoc(11, 16)).toBe('quick');
	});

	describe('inline suggestions', () => {
		const suggestion = (insert: string, id = 'sug') => ({
			from: 6, to: 11, id, suggestion: { quote: 'brown', insert, color: '#e0489e', inline: true },
		});
		// jsdom has no layout: give the caret a box so a selection can report where it is.
		const rangeLayout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
		beforeEach(() => {
			rangeLayout.getClientRects = () => [new DOMRect(10, 20, 8, 16)] as unknown as DOMRectList;
			rangeLayout.getBoundingClientRect = () => new DOMRect(10, 20, 8, 16);
		});
		afterEach(() => { delete rangeLayout.getClientRects; delete rangeLayout.getBoundingClientRect; });
		const open = (props: Record<string, unknown>) => {
			host = document.createElement('div'); document.body.appendChild(host);
			component = mount(MarkdownEditor, { target: host, props: Object.defineProperties({ value: 'quick brown fox' }, Object.getOwnPropertyDescriptors(props)) });
			flushSync();
			return EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
		};

		it('draws the suggestion and repaints it when only its insert changes', () => {
			const highlights = writable([suggestion('red')]);
			const live = fromStore(highlights);
			open({ get highlights() { return live.current; } });
			expect(host.querySelector('.cm-suggestion-del')?.textContent).toBe('brown');
			expect(host.querySelector('.cm-suggestion-insert')?.getAttribute('data-insert')).toBe('red');
			highlights.set([suggestion('blue')]); flushSync();
			expect(host.querySelector('.cm-suggestion-insert')?.getAttribute('data-insert')).toBe('blue');
			highlights.set([]); flushSync();
			expect(host.querySelector('.cm-suggestion-del')).toBeNull();
			expect(host.querySelector('.cm-suggestion-insert')).toBeNull();
		});

		it('focuses the card for a press on the insert, and for a caret in the struck text, marked as a suggestion', () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [suggestion('red')], onAnchorFocus });
			host.querySelector('.cm-suggestion-insert')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
			expect(onAnchorFocus).toHaveBeenLastCalledWith('sug', expect.anything(), true, expect.any(MouseEvent));
			onAnchorFocus.mockClear();
			view.dispatch({ selection: { anchor: 8 } }); flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			expect(onAnchorFocus).toHaveBeenCalledWith('sug', expect.anything(), true, undefined);
			expect(view.state.doc.toString()).toBe('quick brown fox');
		});

		it('where cards open as the phone sheet, a caret in the struck text opens nothing but a press on the insert still does', () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [suggestion('red')], onAnchorFocus, commentsAsSheet: true });
			view.dispatch({ selection: { anchor: 8 } }); flushSync();
			view.dispatch({ selection: { anchor: 9 } }); flushSync();
			expect(onAnchorFocus).not.toHaveBeenCalled();
			host.querySelector('.cm-suggestion-insert')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
			expect(onAnchorFocus).toHaveBeenLastCalledWith('sug', expect.anything(), true, expect.any(MouseEvent));
			expect(view.state.doc.toString()).toBe('quick brown fox');
		});

		it('reports a caret in a plain comment as before, with no suggestion flag and no click', () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus });
			view.dispatch({ selection: { anchor: 8 } }); flushSync();
			expect(onAnchorFocus).toHaveBeenCalledWith('plain', expect.anything(), undefined, undefined);
		});

		it('picks a comment once as the caret arrives in it: typing and the arrow keys inside it edit, and leaving and coming back picks it again', () => {
			const onAnchorFocus = vi.fn(), onCaretAway = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, onCaretAway });
			view.dispatch({ selection: { anchor: 8 }, userEvent: 'select' }); flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			view.dispatch({ changes: { from: 8, insert: 'x' }, selection: { anchor: 9 }, userEvent: 'input.type' }); flushSync();
			view.dispatch({ changes: { from: 8, to: 9 }, selection: { anchor: 8 }, userEvent: 'delete.backward' }); flushSync();
			view.dispatch({ selection: { anchor: 9 }, userEvent: 'select' }); flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			expect(view.state.doc.toString()).toBe('quick brown fox');
			view.dispatch({ selection: { anchor: 2 }, userEvent: 'select' }); flushSync();
			expect(onCaretAway).toHaveBeenCalledOnce();
			view.dispatch({ selection: { anchor: 7 }, userEvent: 'select' }); flushSync();
			expect(onAnchorFocus).toHaveBeenCalledTimes(2);
		});

		it('where cards open as the phone sheet, only a tap that brings the caret into a comment opens it; a caret already there, or moved in by keys, is editing', async () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, commentsAsSheet: true });
			// The press the editor reports once the pointer is up, with the caret where it landed.
			const tap = async (at: number) => {
				view.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, detail: 1 }));
				view.dispatch({ selection: { anchor: at }, userEvent: 'select.pointer' });
				window.dispatchEvent(new MouseEvent('mouseup'));
				await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
				flushSync();
			};
			view.dispatch({ selection: { anchor: 8 }, userEvent: 'select' }); flushSync();
			expect(onAnchorFocus).not.toHaveBeenCalled();
			view.dispatch({ selection: { anchor: 2 }, userEvent: 'select' }); flushSync();
			await tap(8);
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			expect(onAnchorFocus).toHaveBeenCalledWith('plain', expect.anything(), undefined, expect.any(MouseEvent));
			// The sheet closed and the person taps into the same text again to edit it, then types.
			await tap(9);
			view.dispatch({ changes: { from: 9, insert: 'x' }, selection: { anchor: 10 }, userEvent: 'input.type' }); flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			expect(view.state.doc.toString()).toBe('quick broxwn fox');
		});

		it('reports the pointer on a suggestion to the viewer', () => {
			const onSuggestionHover = vi.fn();
			open({ highlights: [suggestion('red')], onSuggestionHover });
			host.querySelector('.cm-suggestion-del')!.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
			expect(onSuggestionHover).toHaveBeenCalledWith('sug');
		});
	});
});
