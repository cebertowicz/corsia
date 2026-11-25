import { expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import { deleteCharBackward } from '@codemirror/commands';
import MarkdownEditor from './MarkdownEditor.svelte';
it('Reading prevents deletion of the selected passage through the actual editor command', async () => {
	Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
	Range.prototype.getBoundingClientRect = () => new DOMRect();
	const host = document.body.appendChild(document.createElement('div'));
	const component = mount(MarkdownEditor, { target: host, props: { value: 'intro passage', reading: true } });
	flushSync();
	try {
		const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
		view.dispatch({ selection: { anchor: 0, head: 5 } });
		deleteCharBackward(view);
		expect(view.state.doc.toString()).toBe('intro passage');
	} finally { await unmount(component); host.remove(); }
});
