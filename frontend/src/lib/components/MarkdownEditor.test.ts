import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import type { ChangeDesc } from '@codemirror/state';
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
		const onEdit = vi.fn((changes: ChangeDesc) => remapComments('artifact', changes));
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
		afterEach(() => { delete rangeLayout.getClientRects; delete rangeLayout.getBoundingClientRect; vi.restoreAllMocks(); });
		// And no pointer position to read: a press lands where the test put the caret, as the browser does under the pointer.
		beforeEach(() => { vi.spyOn(EditorView.prototype, 'posAtCoords').mockImplementation(function (this: EditorView) { return this.state.selection.main.head; }); });
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

		it('takes the new sign off a highlight once its thread is read, with nothing else about it changed', () => {
			const highlights = writable([{ from: 6, to: 11, id: 'plain', focused: true, unread: 'reply' as const }]);
			const live = fromStore(highlights);
			open({ get highlights() { return live.current; } });
			expect(host.querySelector('[data-comment-new="plain"]')).not.toBeNull();
			highlights.set([{ from: 6, to: 11, id: 'plain', focused: true }]); flushSync();
			expect(host.querySelector('[data-comment-new]')).toBeNull();
			expect(host.querySelector('.cm-comment-anchor.cm-comment-focused')?.textContent).toBe('brown');
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
			const view = open({ highlights: [suggestion('red')], onAnchorFocus, cardMode: 'sheet' });
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

		it('where cards open as the phone sheet, every tap on a comment opens it; a caret moved in by keys or typing is editing', async () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, cardMode: 'sheet' });
			// The press the editor reports once the pointer is up, with the caret where it landed.
			const tap = async (at: number) => {
				// The pointer is over `at` when it goes down, as it is in a browser.
				vi.mocked(EditorView.prototype.posAtCoords).mockReturnValue(at);
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
			// The sheet closed and the person types where the tap left the caret; typing opens nothing. A second tap opens it again.
			view.dispatch({ changes: { from: 8, insert: 'x' }, selection: { anchor: 9 }, userEvent: 'input.type' }); flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			await tap(10);
			expect(onAnchorFocus).toHaveBeenCalledTimes(2);
			expect(view.state.doc.toString()).toBe('quick brxown fox');
		});

		it('a tap opens the comment it landed on even when the text moves before the frame that reads it', async () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, cardMode: 'sheet' });
			vi.mocked(EditorView.prototype.posAtCoords).mockReturnValue(8);
			view.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, detail: 1 }));
			view.dispatch({ selection: { anchor: 8 }, userEvent: 'select.pointer' });
			window.dispatchEvent(new MouseEvent('mouseup'));
			// The list reloads and the text shifts: the point under the pointer is now outside the comment.
			vi.mocked(EditorView.prototype.posAtCoords).mockReturnValue(14);
			await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			expect(onAnchorFocus).toHaveBeenCalledWith('plain', expect.anything(), undefined, expect.any(MouseEvent));
		});

		it('a press held for over half a second on a comment opens it when the pointer goes up, as before', async () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, cardMode: 'sheet' });
			vi.mocked(EditorView.prototype.posAtCoords).mockReturnValue(8);
			view.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, detail: 1 }));
			view.dispatch({ selection: { anchor: 8 }, userEvent: 'select.pointer' });
			await new Promise<void>((resolve) => setTimeout(resolve, 600));
			expect(onAnchorFocus).not.toHaveBeenCalled();
			window.dispatchEvent(new MouseEvent('mouseup'));
			await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
		});

		it('a press that drags into a text selection opens no comment', async () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, cardMode: 'sheet' });
			vi.mocked(EditorView.prototype.posAtCoords).mockReturnValue(8);
			view.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, detail: 1 }));
			view.dispatch({ selection: { anchor: 8, head: 14 }, userEvent: 'select.pointer' });
			window.dispatchEvent(new MouseEvent('mouseup'));
			await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			flushSync();
			expect(onAnchorFocus).not.toHaveBeenCalled();
			expect(view.state.selection.main.empty).toBe(false);
		});

		it('where cards open as the phone sheet, keys that carry the caret into a comment before the tap is read open nothing', async () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, cardMode: 'sheet' });
			// A slow frame: the tap lands outside the comment, and the arrow keys move the caret in before the press is read.
			view.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, detail: 1 }));
			view.dispatch({ selection: { anchor: 13 }, userEvent: 'select.pointer' });
			window.dispatchEvent(new MouseEvent('mouseup'));
			view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowLeft', keyCode: 37 }));
			view.dispatch({ selection: { anchor: 8 }, userEvent: 'select' });
			await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			flushSync();
			expect(onAnchorFocus).not.toHaveBeenCalled();
		});

		it('where cards open as the phone sheet, a tap whose caret is reported late, as a plain select after the pointer is up, still opens its comment once', async () => {
			const onAnchorFocus = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, cardMode: 'sheet' });
			// No pointer selection: a touch leaves the caret to the browser, whose report CodeMirror labels 'select' once 50 ms have passed.
			vi.mocked(EditorView.prototype.posAtCoords).mockReturnValue(8);
			view.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, detail: 1 }));
			window.dispatchEvent(new MouseEvent('mouseup'));
			view.dispatch({ selection: { anchor: 8 }, userEvent: 'select' }); flushSync();
			await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			flushSync();
			expect(onAnchorFocus).toHaveBeenCalledOnce();
			expect(onAnchorFocus).toHaveBeenCalledWith('plain', expect.anything(), undefined, expect.any(MouseEvent));
		});

		describe('one rule for the highlight the reader is in: a press decides until a key or a later press moves the caret', () => {
			const frame = async () => { await new Promise<void>((resolve) => requestAnimationFrame(() => resolve())); flushSync(); };
			/** A press at `x`, `y` (the row is 20 to 36, the caret's box 10 to 18) that the editor reads a frame after the pointer is up; `at` is the position under it. */
			const press = async (view: EditorView, at: number, x: number, y = 25) => {
				vi.mocked(EditorView.prototype.posAtCoords).mockReturnValue(at);
				view.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, detail: 1, clientX: x, clientY: y }));
				view.dispatch({ selection: { anchor: at }, userEvent: 'select.pointer' });
				window.dispatchEvent(new MouseEvent('mouseup'));
				await frame();
			};
			const key = (view: EditorView, name: string) => view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: name }));

			it('a phone tap on a highlight that stops short of its word opens it and the card stays open when the browser then reports the caret at the word end', async () => {
				const onAnchorFocus = vi.fn(), onCaretAway = vi.fn();
				const view = open({ highlights: [{ from: 6, to: 9, id: 'stem' }], onAnchorFocus, onCaretAway, cardMode: 'sheet' });
				await press(view, 7, 14);
				expect(onAnchorFocus).toHaveBeenCalledOnce();
				// The caret the press left with the browser arrives as a plain select, at the end of the tapped word: outside the highlight.
				view.dispatch({ selection: { anchor: 11 }, userEvent: 'select' }); flushSync();
				view.dispatch({ selection: { anchor: 11 }, userEvent: 'select' }); flushSync();
				expect(onCaretAway).not.toHaveBeenCalled();
				expect(onAnchorFocus).toHaveBeenCalledOnce();
			});

			it('a press that opened nothing leaves the caret beside a highlight, and typing or deleting there opens nothing', async () => {
				const onAnchorFocus = vi.fn();
				const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus });
				// Right after the highlighted word, on its row but past its last character: no highlight, the caret at its end.
				await press(view, 11, 100);
				expect(onAnchorFocus).not.toHaveBeenCalled();
				key(view, 'Backspace');
				view.dispatch({ changes: { from: 10, to: 11 }, selection: { anchor: 10 }, userEvent: 'delete.backward' }); flushSync();
				expect(onAnchorFocus).not.toHaveBeenCalled();
				// And before it: a press left of its first character, then a typed letter that carries the caret in.
				await press(view, 6, 0);
				key(view, 'x');
				view.dispatch({ changes: { from: 6, insert: 'x' }, selection: { anchor: 7 }, userEvent: 'input.type' }); flushSync();
				expect(onAnchorFocus).not.toHaveBeenCalled();
			});

			// A press opened `brown`; then something that is not a key moves the caret. It no longer holds the card the press opened.
			const afterPress = async () => {
				const onAnchorFocus = vi.fn(), onCaretAway = vi.fn();
				const view = open({ highlights: [{ from: 0, to: 5, id: 'quick' }, { from: 6, to: 11, id: 'brown' }], onAnchorFocus, onCaretAway });
				await press(view, 8, 14);
				expect(onAnchorFocus).toHaveBeenCalledOnce();
				return { view, onAnchorFocus, onCaretAway };
			};

			it.each([
				['a paste with no key before it', 'input.paste'],
				['an autocorrect or dictation compose', 'input.type.compose'],
				['an undo', 'undo'],
			])('%s that carries the caret out of the highlight a press opened releases its card, and typing into another picks none', async (_name, event) => {
				const { view, onAnchorFocus, onCaretAway } = await afterPress();
				view.dispatch({ changes: { from: 15, insert: 'x' }, selection: { anchor: 16 }, userEvent: event }); flushSync();
				expect(onCaretAway).toHaveBeenCalledOnce();
				// The same change landing inside another highlight is editing there, not arriving in it.
				view.dispatch({ changes: { from: 2, insert: 'y' }, selection: { anchor: 3 }, userEvent: event }); flushSync();
				expect(onAnchorFocus).toHaveBeenCalledOnce();
			});

			it('a caret move that is not a key and not the press (a programmatic or voice move) releases the card, and moving into another highlight picks it', async () => {
				const { view, onAnchorFocus, onCaretAway } = await afterPress();
				view.dispatch({ selection: { anchor: 14 }, userEvent: 'move' }); flushSync();
				expect(onCaretAway).toHaveBeenCalledOnce();
				view.dispatch({ selection: { anchor: 3 }, userEvent: 'move' }); flushSync();
				expect(onAnchorFocus).toHaveBeenCalledTimes(2);
				expect(onAnchorFocus).toHaveBeenLastCalledWith('quick', expect.anything(), undefined, undefined);
			});

			it('a selection-only report with no user event after a press is still the press\'s own caret and releases nothing', async () => {
				const { view, onAnchorFocus, onCaretAway } = await afterPress();
				view.dispatch({ selection: { anchor: 14 } }); flushSync();
				view.dispatch({ selection: { anchor: 14 }, userEvent: 'select' }); flushSync();
				expect(onCaretAway).not.toHaveBeenCalled();
				expect(onAnchorFocus).toHaveBeenCalledOnce();
			});

			it('a caret that a key carries into a highlight after a press opens it, and a later press decides again', async () => {
				const onAnchorFocus = vi.fn();
				const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus });
				await press(view, 2, 0);
				key(view, 'ArrowRight');
				view.dispatch({ selection: { anchor: 8 }, userEvent: 'select' }); flushSync();
				expect(onAnchorFocus).toHaveBeenCalledOnce();
				// A press on the highlight now is a new answer, not the arrow key's.
				await press(view, 12, 100);
				key(view, 'ArrowLeft');
				view.dispatch({ selection: { anchor: 9 }, userEvent: 'select' }); flushSync();
				expect(onAnchorFocus).toHaveBeenCalledTimes(2);
			});
		});

		it('where cards open as a popover over the text, the caret arriving by keys opens none, and typing is reported so an open one can close', () => {
			const onAnchorFocus = vi.fn(), onTyping = vi.fn();
			const view = open({ highlights: [{ from: 6, to: 11, id: 'plain' }], onAnchorFocus, onTyping, cardMode: 'popover' });
			view.dispatch({ selection: { anchor: 8 }, userEvent: 'select' }); flushSync();
			expect(onAnchorFocus).not.toHaveBeenCalled();
			view.dispatch({ changes: { from: 8, insert: 'x' }, selection: { anchor: 9 }, userEvent: 'input.type' }); flushSync();
			expect(onTyping).toHaveBeenCalledOnce();
			view.dispatch({ changes: { from: 8, to: 9 } }); flushSync();
			expect(onTyping).toHaveBeenCalledOnce();
		});

		it('reports the pointer on a suggestion to the viewer', () => {
			const onSuggestionHover = vi.fn();
			open({ highlights: [suggestion('red')], onSuggestionHover });
			host.querySelector('.cm-suggestion-del')!.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
			expect(onSuggestionHover).toHaveBeenCalledWith('sug');
		});
	});
});
