import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import { EditorView } from '@codemirror/view';

vi.mock('$lib/api/docs', () => ({ fetchComments: vi.fn(), postComment: vi.fn(), patchComment: vi.fn(), persistPositions: vi.fn() }));

import MarkdownEditor from './MarkdownEditor.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

/** A non-live editor holding `initial`, a typing helper, an undo key and the text store the file's text arrives through. */
function openEditor(initial: string) {
	Range.prototype.getClientRects ??= () => [] as unknown as DOMRectList;
	const text = writable(initial);
	const live = fromStore(text);
	const edits: string[] = [];
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(MarkdownEditor, { target: host, props: Object.defineProperties({ onEdit: () => edits.push('edit') }, Object.getOwnPropertyDescriptors({
		get value() { return live.current; }, set value(next: string) { text.set(next); },
	})) });
	flushSync();
	const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	const mod = /Mac/.test(navigator.platform) ? 'metaKey' : 'ctrlKey';
	return {
		view, text, edits,
		type: (at: number, insert: string) => { view.dispatch({ changes: { from: at, insert }, userEvent: 'input.type' }); flushSync(); },
		undo: () => { view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 'z', [mod]: true, bubbles: true, cancelable: true })); flushSync(); },
	};
}

// Every way the file's text changes under a non-live editor (an agent's edit, a reload, "Use the file's text", the setText fallback)
// arrives as a new `value`, so these three cases stand for all of them.
describe('MarkdownEditor undo after the file changed under it', () => {
	const cases: [string, string, number, string, string][] = [
		['an agent edit away from the typing', 'alpha beta gamma', 0, 'Xalpha beta gamma delta', 'X'],
		['an agent edit over the typed span', 'alpha beta gamma', 6, 'alpha AGENT beta gamma', 'NEW '],
		['a reload to a different file', 'alpha beta gamma', 0, 'completely different file', 'X'],
	];
	it.each(cases)('never changes the synced text: %s', (_name, initial, at, synced, typed) => {
		const { view, text, edits, type, undo } = openEditor(initial);
		type(at, typed);
		text.set(synced); flushSync();
		expect(view.state.doc.toString()).toBe(synced);
		const editsBefore = edits.length;
		undo(); undo();
		expect(view.state.doc.toString()).toBe(synced);
		expect(edits.length).toBe(editsBefore);
	});
});
