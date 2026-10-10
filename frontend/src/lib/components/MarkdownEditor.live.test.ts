import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import { StateEffect } from '@codemirror/state';
import * as Y from 'yjs';
import MarkdownEditor from './MarkdownEditor.svelte';

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
const docs: Y.Doc[] = [];

function documentText(text = ''): Y.Text {
	const doc = new Y.Doc();
	docs.push(doc);
	const value = doc.getText('text');
	if (text) value.insert(0, text);
	return value;
}

function editor(text: Y.Text, live = { current: text }): EditorView {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MarkdownEditor, {
		target: host,
		props: { value: text.toString(), get liveText() { return live.current; } },
	});
	flushSync();
	return EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
}

beforeEach(() => {
	// jsdom has no layout; text, transactions and selection mapping remain real.
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	for (const doc of docs.splice(0)) doc.destroy();
	vi.restoreAllMocks();
});

describe('MarkdownEditor live document binding', () => {
	it('rebinds a changed store without writing either snapshot into the other document', () => {
		const old = documentText('device copy');
		const next = documentText('reset server');
		const live = writable(old);
		const view = editor(old, fromStore(live));
		view.dispatch({ changes: { from: 0, insert: 'old ' }, userEvent: 'input.type' });
		live.set(next);
		flushSync();
		expect(view.state.doc.toString()).toBe('reset server');
		expect(next.toString()).toBe('reset server');
		expect(old.toString()).toBe('old device copy');
		old.insert(0, 'late ');
		flushSync();
		expect(view.state.doc.toString()).toBe('reset server');
		view.dispatch({ changes: { from: 0, insert: 'new ' }, userEvent: 'input.type' });
		expect(next.toString()).toBe('new reset server');
		expect(old.toString()).toBe('late old device copy');
	});

	it('writes actual editor transactions into Y.Text, including deletions', () => {
		const text = documentText('The quick fox');
		const view = editor(text);
		view.dispatch({ changes: { from: 4, to: 10, insert: 'slow ' }, userEvent: 'input.type' });
		flushSync();
		expect(text.toString()).toBe('The slow fox');
		view.dispatch({ changes: { from: 4, to: 9 }, userEvent: 'delete.backward' });
		flushSync();
		expect(text.toString()).toBe('The fox');
	});

	it('applies remote deltas without whole-text replacement and maps the selection', () => {
		const text = documentText('quick fox');
		const remote = documentText();
		Y.applyUpdate(remote.doc!, Y.encodeStateAsUpdate(text.doc!));
		const view = editor(text);
		view.dispatch({ selection: { anchor: 6 } });
		const changes: { from: number; to: number; insert: string }[] = [];
		view.dispatch({ effects: StateEffect.appendConfig.of(EditorView.updateListener.of(update => {
			update.changes.iterChanges((from, to, _nextFrom, _nextTo, insert) => {
				changes.push({ from, to, insert: insert.toString() });
			});
		})) });
		const observer = vi.fn();
		text.observe(observer);
		remote.insert(0, 'The ');
		Y.applyUpdate(text.doc!, Y.encodeStateAsUpdate(remote.doc!), 'remote');
		flushSync();
		expect(view.state.doc.toString()).toBe('The quick fox');
		expect(view.state.selection.main.anchor).toBe(10);
		expect(observer).toHaveBeenCalledOnce();
		text.unobserve(observer);
		expect(changes).toEqual([{ from: 0, to: 0, insert: 'The ' }]);
	});

	it('undo removes only local typing and keeps a remote edit', () => {
		const text = documentText('fox');
		const remote = documentText();
		Y.applyUpdate(remote.doc!, Y.encodeStateAsUpdate(text.doc!));
		const view = editor(text);
		view.dispatch({ changes: { from: 0, insert: 'quick ' }, userEvent: 'input.type' });
		remote.insert(3, '!');
		Y.applyUpdate(text.doc!, Y.encodeStateAsUpdate(remote.doc!), 'remote');
		flushSync();
		expect(view.state.doc.toString()).toBe('quick fox!');
		view.contentDOM.dispatchEvent(new InputEvent('beforeinput', { inputType: 'historyUndo', bubbles: true, cancelable: true }));
		flushSync();
		expect(text.toString()).toBe('fox!');
		expect(view.state.doc.toString()).toBe('fox!');
		expect(host.querySelector('.yRemoteSelection')).toBeNull();
	});
});
