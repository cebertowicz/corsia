import { afterEach, describe, expect, it } from 'vitest';
import * as Y from 'yjs';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { selectComment } from '$lib/stores/commentSelection';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import { pendingCommentMark, setPendingComment } from './pending-comment-mark';

const DOC = 'alpha beta gamma';
const views: EditorView[] = [];

/** A live document: the Y text is the truth and the editor mirrors each Y edit, as the document binding does. */
function mount() {
	const doc = new Y.Doc(), text = doc.getText('text');
	text.insert(0, DOC);
	const view = new EditorView({ state: EditorState.create({ doc: DOC, extensions: [pendingCommentMark] }), parent: document.body });
	views.push(view);
	const selection = selectComment({ doc, text } as unknown as LiveDocumentSession, 6, 10);
	const insert = (at: number, value: string) => { text.insert(at, value); view.dispatch({ changes: { from: at, insert: value } }); };
	const remove = (from: number, to: number) => { text.delete(from, to - from); view.dispatch({ changes: { from, to } }); };
	const marked = () => [...view.dom.querySelectorAll('.cm-comment-pending')].map((node) => node.textContent).join('');
	return { view, selection, insert, remove, marked };
}

afterEach(() => { views.splice(0).forEach((view) => view.destroy()); document.body.innerHTML = ''; });

describe('pending comment highlight', () => {
	it('shows the captured range once the composer captures it, never as a placed comment anchor', () => {
		const { view, selection, marked } = mount();
		expect(marked()).toBe('');
		view.dispatch({ effects: setPendingComment.of(selection) });
		expect(marked()).toBe('beta');
		expect(view.dom.querySelector('.cm-comment-anchor')).toBeNull();
	});

	it('follows a remote insert before the range', () => {
		const { view, selection, insert, marked } = mount();
		view.dispatch({ effects: setPendingComment.of(selection) });
		insert(0, 'XX ');
		expect(view.state.doc.sliceString(9, 13)).toBe('beta');
		expect(marked()).toBe('beta');
	});

	it('goes away when a remote edit inside the range changes the quoted text', () => {
		const { view, selection, insert, marked } = mount();
		view.dispatch({ effects: setPendingComment.of(selection) });
		insert(8, 'Z');
		expect(marked()).toBe('');
	});

	it('clears on cancel and on post, when the draft drops its selection', () => {
		const { view, selection, marked } = mount();
		view.dispatch({ effects: setPendingComment.of(selection) });
		expect(marked()).toBe('beta');
		view.dispatch({ effects: setPendingComment.of(null) });
		expect(marked()).toBe('');
	});

	it('disappears when the range is removed', () => {
		const { view, selection, remove, marked } = mount();
		view.dispatch({ effects: setPendingComment.of(selection) });
		remove(6, 10);
		expect(marked()).toBe('');
	});
});
