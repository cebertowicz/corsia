import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { commentMarks, commentRanges as commentRangesField, setCommentRanges, type CommentRange, type InlineSuggestion } from './comment-marks';
import { suggestionInsertShown, suggestionMarks, type SuggestionPointer } from './suggestion-marks';

const DOC = 'Before the meeting closed, The team met on Tuesday. Everyone agreed.';
const QUOTE = 'The team met on Tuesday.';
const FROM = DOC.indexOf(QUOTE);
const TO = FROM + QUOTE.length;

const views: EditorView[] = [];
const pointer: SuggestionPointer = { focus: vi.fn(), hover: vi.fn() };

function mount(ranges: CommentRange[], extra: unknown[] = [], doc = DOC) {
	const view = new EditorView({
		state: EditorState.create({ doc, extensions: [commentMarks, suggestionMarks(() => pointer), ...extra as never[]] }),
		parent: document.body,
	});
	views.push(view);
	view.dispatch({ effects: setCommentRanges.of(ranges) });
	return view;
}

const suggestion = (overrides: Partial<InlineSuggestion> = {}): InlineSuggestion =>
	({ quote: QUOTE, insert: 'The team postponed planting.', color: '#e0489e', inline: true, ...overrides });
const range = (overrides: Partial<CommentRange> = {}, s: Partial<InlineSuggestion> = {}): CommentRange =>
	({ from: FROM, to: TO, id: 's1', suggestion: suggestion(s), ...overrides });
const strikes = (view: EditorView) => [...view.dom.querySelectorAll<HTMLElement>('.cm-suggestion-del')];
const inserts = (view: EditorView) => [...view.dom.querySelectorAll<HTMLElement>('.cm-suggestion-insert')];

afterEach(() => {
	views.splice(0).forEach((view) => view.destroy());
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('inline suggestion marks', () => {
	it('keeps the replaced text in place, struck, with the insert right after it, and leaves the document alone', () => {
		const view = mount([range()]);
		const [strike] = strikes(view);
		expect(strike.textContent).toBe(QUOTE);
		expect(strike.getAttribute('data-suggestion-id')).toBe('s1');
		expect(strike.style.getPropertyValue('--suggestion-color')).toBe('#e0489e');
		expect(strike.style.getPropertyValue('--suggestion-tone-dark')).toMatch(/^#[0-9a-f]{6}$/);
		expect(strike.style.getPropertyValue('--suggestion-tone-light')).toMatch(/^#[0-9a-f]{6}$/);
		const [insert] = inserts(view);
		expect(insert.getAttribute('data-insert')).toBe('The team postponed planting.');
		// Right after the struck text (CodeMirror's own widget buffer may sit between), before the rest of the sentence.
		expect(strike.compareDocumentPosition(insert) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(strike.parentElement).toBe(insert.parentElement);
		expect(insert.parentElement!.textContent).toBe(DOC);
		expect(view.state.doc.toString()).toBe(DOC);
	});

	it('draws no amber anchor on a suggestion range, and keeps it on a plain comment', () => {
		const view = mount([range(), { from: 0, to: 6, id: 'plain' }]);
		expect(view.dom.querySelectorAll('.cm-comment-anchor')).toHaveLength(1);
		expect(view.dom.querySelector('.cm-comment-anchor')!.textContent).toBe('Before');
	});

	it('makes the insert something other than document text', () => {
		const view = mount([range()]);
		const [insert] = inserts(view);
		expect(insert.contentEditable).toBe('false');
		// The words live in an attribute drawn by CSS, so there is no text to select, copy or find.
		expect(insert.textContent).toBe('');
		expect(view.state.doc.toString()).not.toContain('postponed');
		expect(view.contentDOM.textContent).not.toContain('postponed');
		const css = [...document.querySelectorAll('style')].map((style) => style.textContent).join('\n');
		expect(css).toMatch(/\.cm-suggestion-insert::after\s*\{[^}]*content:\s*attr\(data-insert\)/);
		expect(css).toMatch(/\.cm-suggestion-insert\s*\{[^}]*user-select:\s*none/);
	});

	it('hides the drawn insert from assistive tech and leaves the struck document text readable', () => {
		const view = mount([range()]);
		expect(inserts(view)[0].getAttribute('aria-hidden')).toBe('true');
		expect(strikes(view)[0].hasAttribute('aria-hidden')).toBe(false);
	});

	it('shows only the insert for a pure insertion, at the anchor, with no gap', () => {
		const view = mount([{ from: FROM, to: FROM, id: 's1', suggestion: suggestion({ quote: '', insert: 'Note: ' }) }]);
		expect(strikes(view)).toHaveLength(0);
		const [insert] = inserts(view);
		expect(insert.getAttribute('data-insert')).toBe('Note: ');
		expect(insert.classList.contains('cm-suggestion-insert-after')).toBe(false);
		expect(view.state.field(commentRangesField)).toHaveLength(1);
	});

	it('shows only the strike for a pure deletion', () => {
		const view = mount([range({}, { insert: '' })]);
		expect(strikes(view)).toHaveLength(1);
		expect(inserts(view)).toHaveLength(0);
	});

	it('puts a 0.25em presentation-only gap between the strike and its insert', () => {
		const view = mount([range()]);
		expect(inserts(view)[0].classList.contains('cm-suggestion-insert-after')).toBe(true);
		const css = [...document.querySelectorAll('style')].map((style) => style.textContent).join('\n');
		expect(css).toMatch(/\.cm-suggestion-insert-after\s*\{[^}]*margin-left:\s*0\.25em/);
		// The gap is a margin: no space character reaches the document or the copy.
		expect(view.state.doc.toString()).toBe(DOC);
	});

	it('breaks between words and falls back to anywhere only for one unbreakable token', () => {
		mount([range()]);
		const css = [...document.querySelectorAll('style')].map((style) => style.textContent).join('\n');
		expect(css).toMatch(/\.cm-suggestion-insert\s*\{[^}]*word-break:\s*normal/);
		expect(css).toMatch(/\.cm-suggestion-insert\s*\{[^}]*overflow-wrap:\s*anywhere/);
		expect(css).not.toMatch(/\.cm-suggestion-insert\s*\{[^}]*word-break:\s*break-all/);
	});

	it('tints both parts of a focused suggestion in the light and dark themes', () => {
		const view = mount([range({ focused: true })]);
		expect(strikes(view)[0].classList.contains('cm-suggestion-focus')).toBe(true);
		expect(inserts(view)[0].classList.contains('cm-suggestion-focus')).toBe(true);
		const css = [...document.querySelectorAll('style')].map((style) => style.textContent).join('\n');
		expect(css).toMatch(/\.cm-suggestion-focus\s*\{[^}]*12%/);
		expect(css).toMatch(/data-theme="light"\]\s+\S+\s+\.cm-suggestion-focus\s*\{[^}]*8%/);
	});

	it('draws an overlapped suggestion nowhere, until its card is focused: then only the tint', () => {
		const view = mount([range({ id: 'second' }, { inline: false })]);
		expect(strikes(view)).toHaveLength(0);
		expect(inserts(view)).toHaveLength(0);
		expect(view.dom.querySelector('.cm-suggestion-focus')).toBeNull();
		view.dispatch({ effects: setCommentRanges.of([range({ id: 'second', focused: true }, { inline: false })]) });
		const tint = view.dom.querySelectorAll('.cm-suggestion-focus');
		expect(tint).toHaveLength(1);
		expect(tint[0].textContent).toBe(QUOTE);
		expect(tint[0].classList.contains('cm-suggestion-del')).toBe(false);
		expect(inserts(view)).toHaveLength(0);
	});

	it('keeps the marks, marked busy, while the Accept is in flight', () => {
		const view = mount([range({}, { busy: true })]);
		expect(strikes(view)[0].getAttribute('aria-busy')).toBe('true');
		expect(inserts(view)[0].getAttribute('aria-busy')).toBe('true');
		expect(strikes(view)[0].textContent).toBe(QUOTE);
		view.dispatch({ effects: setCommentRanges.of([range()]) });
		expect(strikes(view)[0].hasAttribute('aria-busy')).toBe(false);
	});

	it('removes the marks when the suggestion is no longer in the ranges (accepted, rejected, decided elsewhere)', () => {
		const view = mount([range()]);
		view.dispatch({ effects: setCommentRanges.of([]) });
		expect(strikes(view)).toHaveLength(0);
		expect(inserts(view)).toHaveLength(0);
	});

	it('shows the marks in a read-only session', () => {
		const view = mount([range()], [EditorState.readOnly.of(true)]);
		expect(strikes(view)).toHaveLength(1);
		expect(inserts(view)).toHaveLength(1);
	});

	it('ends the suggestion on the first keystroke that changes the quoted text', () => {
		const view = mount([range()]);
		view.dispatch({ changes: { from: FROM + 4, insert: 'x' }, userEvent: 'input.type' });
		expect(strikes(view)).toHaveLength(0);
		expect(inserts(view)).toHaveLength(0);
		expect(view.state.field(commentRangesField)).toHaveLength(0);
	});

	it('keeps the suggestion, moved, when the edit is before, after or beside the quote', () => {
		const view = mount([range()]);
		view.dispatch({ changes: { from: 0, insert: 'Note. ' } });
		view.dispatch({ changes: { from: view.state.doc.length, insert: ' More.' } });
		expect(strikes(view)).toHaveLength(1);
		expect(strikes(view)[0].textContent).toBe(QUOTE);
		// A character typed at the end of the quote is outside it.
		view.dispatch({ changes: { from: FROM + 6 + QUOTE.length, insert: '!' } });
		expect(strikes(view)[0].textContent).toBe(QUOTE);
		expect(inserts(view)).toHaveLength(1);
	});

	it('shows the whole insert at once: nothing is revealed progressively', () => {
		const reduced = vi.spyOn(window, 'matchMedia').mockImplementation((query) => ({
			matches: true, media: query, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
		}) as unknown as MediaQueryList);
		try {
			const view = mount([range()]);
			expect(inserts(view)[0].getAttribute('data-insert')).toBe('The team postponed planting.');
		} finally { reduced.mockRestore(); }
	});

	it('routes the insert text through the typed-reveal seam', () => {
		expect(suggestionInsertShown('whole insert')).toBe('whole insert');
	});
});

describe('suggestion pointer behavior', () => {
	it('focuses the card when the insert is pressed, without moving the caret or editing', () => {
		const view = mount([range()]);
		view.dispatch({ selection: { anchor: 3 } });
		const press = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
		inserts(view)[0].dispatchEvent(press);
		expect(pointer.focus).toHaveBeenCalledWith('s1', expect.objectContaining({ left: 0, top: 0 }));
		expect(press.defaultPrevented).toBe(true);
		expect(view.state.selection.main.head).toBe(3);
		expect(view.state.doc.toString()).toBe(DOC);
	});

	it('leaves a press on the struck text to place the caret', () => {
		const view = mount([range()]);
		strikes(view)[0].dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
		// The caret placement is the editor's own, and it reaches the card through the selection.
		expect(pointer.focus).not.toHaveBeenCalled();
	});

	it('reports the pointer entering and leaving a suggestion, not moving between its parts', () => {
		const view = mount([range()]);
		const [strike] = strikes(view), [insert] = inserts(view);
		strike.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
		expect(pointer.hover).toHaveBeenLastCalledWith('s1');
		strike.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: insert }));
		expect(pointer.hover).toHaveBeenCalledTimes(1);
		insert.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body }));
		expect(pointer.hover).toHaveBeenLastCalledWith(null);
	});
});

