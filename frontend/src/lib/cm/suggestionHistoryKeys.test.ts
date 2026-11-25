import { describe, expect, it, vi } from 'vitest';
import { history } from '@codemirror/commands';
import { Compartment, EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { acceptedSuggestion, suggestionHistory } from './suggestionHistory';
import { suggestionHistoryKeys } from './suggestionHistoryKeys';

Range.prototype.getClientRects ??= () => [] as unknown as DOMRectList;
const mod = /Mac/.test(navigator.platform) ? 'metaKey' : 'ctrlKey';
function press(view: EditorView, key: string, shiftKey = false) {
	view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key, [mod]: true, shiftKey, bubbles: true, cancelable: true }));
}
function editor(transition: Parameters<typeof suggestionHistoryKeys>[0], recover = vi.fn()) {
	const view = new EditorView({ doc: 'old', parent: document.body, extensions: [history(), suggestionHistory, suggestionHistoryKeys(transition, recover)] });
	view.dispatch({ changes: { from: 0, to: 3, insert: 'new' }, ...acceptedSuggestion('event', ['s']) });
	return view;
}

describe('suggestion history keys', () => {
	it('holds the text until the server succeeds and then supports redo', async () => {
		let settle!: (value: boolean) => void;
		const transition = vi.fn().mockImplementationOnce(() => new Promise<boolean>(resolve => { settle = resolve; })).mockResolvedValueOnce(true);
		const view = editor(transition);
		press(view, 'z');
		expect(view.state.doc.toString()).toBe('new');
		expect(view.state.readOnly).toBe(true);
		expect(view.contentDOM.getAttribute('contenteditable')).toBe('true');
		settle(true);
		await vi.waitFor(() => expect(view.state.doc.toString()).toBe('old'));
		expect(view.state.readOnly).toBe(false);
		press(view, mod === 'metaKey' ? 'z' : 'y', mod === 'metaKey');
		await vi.waitFor(() => expect(view.state.doc.toString()).toBe('new'));
		expect(transition.mock.calls.map(call => call[0].direction)).toEqual(['undo', 'redo']);
		view.destroy();
	});

	it('recovers if the document changes while the server is writing', async () => {
		let settle!: (value: boolean) => void;
		const recover = vi.fn();
		const view = editor(() => new Promise<boolean>(resolve => { settle = resolve; }), recover);
		press(view, 'z');
		view.dispatch({ changes: { from: 0, insert: 'outside ' } });
		settle(true);
		await vi.waitFor(() => expect(recover).toHaveBeenCalledOnce());
		expect(view.state.doc.toString()).toBe('outside new');
		view.destroy();
	});

	it('keeps history and text unchanged when the server refuses', async () => {
		const transition = vi.fn().mockResolvedValue(false);
		const view = editor(transition);
		press(view, 'z');
		await vi.waitFor(() => expect(view.state.readOnly).toBe(false));
		expect(view.state.doc.toString()).toBe('new');
		press(view, 'z');
		await vi.waitFor(() => expect(transition).toHaveBeenCalledTimes(2));
		view.destroy();
	});

	it('makes no change and no server write in a read-only editor, such as phone Reading', async () => {
		const transition = vi.fn().mockResolvedValue(true);
		const lock = new Compartment();
		const view = new EditorView({ doc: 'old', parent: document.body, extensions: [history(), suggestionHistory, suggestionHistoryKeys(transition, vi.fn()), lock.of([])] });
		view.dispatch({ changes: { from: 0, to: 3, insert: 'new' }, ...acceptedSuggestion('event', ['s']) });
		view.dispatch({ effects: lock.reconfigure(EditorState.readOnly.of(true)) });
		press(view, 'z');
		expect(view.state.doc.toString()).toBe('new');
		view.dispatch({ effects: lock.reconfigure([]) });
		press(view, 'z');
		await vi.waitFor(() => expect(view.state.doc.toString()).toBe('old'));
		expect(transition).toHaveBeenCalledTimes(1);
		view.dispatch({ effects: lock.reconfigure(EditorState.readOnly.of(true)) });
		press(view, mod === 'metaKey' ? 'z' : 'y', mod === 'metaKey');
		expect(view.state.doc.toString()).toBe('old');
		expect(transition).toHaveBeenCalledTimes(1);
		view.destroy();
	});
});
