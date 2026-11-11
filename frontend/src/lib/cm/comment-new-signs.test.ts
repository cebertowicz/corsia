import { afterEach, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { commentMarks, paintSignature, setCommentRanges } from './comment-marks';
import { commentNewSigns } from './comment-new-signs';

vi.mock('$lib/documentReadingPlace', () => ({ releasePlace: vi.fn() }));

let view: EditorView | null = null;

afterEach(() => {
	view?.destroy();
	view = null;
	document.body.replaceChildren();
});

const signs = () => document.querySelectorAll('[data-highlight-dot] [aria-label="New reply"]');
const flush = (editor: EditorView) => (editor as unknown as { measure: () => void }).measure();

it('pins the new-reply sign on an unread thread\'s highlight, and takes it away once the thread is read', () => {
	view = new EditorView({ state: EditorState.create({ doc: 'The backup runs at 2 AM nightly.', extensions: [commentMarks, commentNewSigns] }), parent: document.body.appendChild(document.createElement('div')) });
	view.dispatch({ effects: setCommentRanges.of([{ from: 11, to: 23, id: 'asked', unread: 'reply' }, { from: 0, to: 3, id: 'quiet' }]) });
	flush(view);

	// Only the unread thread's highlight names it, and only it carries the sign; the sign sits outside the text.
	expect(view.contentDOM.querySelector('[data-comment-new="asked"]')?.textContent).toBe('runs at 2 AM');
	expect(view.contentDOM.querySelector('[data-comment-new="quiet"]')).toBeNull();
	expect(signs()).toHaveLength(1);
	expect(view.contentDOM.querySelector('[data-highlight-dot]')).toBeNull();

	view.dispatch({ effects: setCommentRanges.of([{ from: 11, to: 23, id: 'asked' }, { from: 0, to: 3, id: 'quiet' }]) });
	flush(view);
	expect(signs()).toHaveLength(0);
});

it('finds an image comment\'s sign when the highlight is a pin', () => {
	view = new EditorView({ state: EditorState.create({ doc: 'Diagram below.', extensions: [commentMarks, commentNewSigns] }), parent: document.body.appendChild(document.createElement('div')) });
	const pin = view.scrollDOM.appendChild(Object.assign(document.createElement('button'), { className: 'cm-image-pin' }));
	pin.dataset.commentId = 'region';
	view.dispatch({ effects: setCommentRanges.of([{ from: 0, to: 7, id: 'region', region: true, unread: 'reply' }]) });
	flush(view);
	expect(signs()).toHaveLength(1);
});

it('names the sign for what is new: a new comment with no new reply is a New comment, then a New reply once one arrives', () => {
	view = new EditorView({ state: EditorState.create({ doc: 'The backup runs at 2 AM nightly.', extensions: [commentMarks, commentNewSigns] }), parent: document.body.appendChild(document.createElement('div')) });
	const named = () => [...document.querySelectorAll('[data-highlight-dot] [role="img"]')].map((sign) => sign.getAttribute('aria-label'));
	view.dispatch({ effects: setCommentRanges.of([{ from: 11, to: 23, id: 'fresh', unread: 'comment' }]) });
	flush(view);
	expect(named()).toEqual(['New comment']);
	view.dispatch({ effects: setCommentRanges.of([{ from: 11, to: 23, id: 'fresh', unread: 'reply' }]) });
	flush(view);
	expect(named()).toEqual(['New reply']);
});

it('repaints a thread that was read: the signature the editor repaints on changes with the unread flag', () => {
	const unread = [{ from: 11, to: 23, id: 'asked', unread: 'reply' as const }, { from: 0, to: 3, id: 'quiet' }];
	const read = [{ from: 11, to: 23, id: 'asked' }, { from: 0, to: 3, id: 'quiet' }];
	expect(paintSignature(read)).not.toBe(paintSignature(unread));
	expect(paintSignature(unread.map((r) => ({ ...r })))).toBe(paintSignature(unread));
	// A new comment is not a new reply: the sign's name changes, so the signature does.
	expect(paintSignature([{ ...unread[0], unread: 'comment' as const }, unread[1]])).not.toBe(paintSignature(unread));
});
