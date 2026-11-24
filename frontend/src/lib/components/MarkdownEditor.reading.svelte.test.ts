import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import { deleteCharBackward } from '@codemirror/commands';
import MarkdownEditor from './MarkdownEditor.svelte';
import { selectComment, type CommentSelection } from '$lib/stores/commentSelection';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import * as Y from 'yjs';

const DOC = 'intro\n\n| Item | Owner |\n|---|---|\n| Alpha | Dana |\n\nafter\n';
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
beforeEach(() => {
	Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
	Range.prototype.getBoundingClientRect = () => new DOMRect();
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	document.getSelection()?.removeAllRanges();
	vi.restoreAllMocks();
	delete (Range.prototype as Partial<Range>).getClientRects;
	delete (Range.prototype as Partial<Range>).getBoundingClientRect;
});
function render() {
	host = document.body.appendChild(document.createElement('div'));
	const props = $state({ value: DOC, reading: true, pendingComment: null as CommentSelection | null });
	component = mount(MarkdownEditor, { target: host, props });
	flushSync();
	const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	return { props, view };
}
describe('Reading editor input and native table selection', () => {
	it('prevents the actual delete command from replacing a selected passage', () => {
		const { view } = render();
		view.dispatch({ selection: { anchor: 0, head: 5 } });
		deleteCharBackward(view);
		expect(view.state.doc.toString()).toBe(DOC);
		expect(view.state.readOnly).toBe(true);
		expect(view.state.facet(EditorView.editable)).toBe(false);
		expect(view.contentDOM.getAttribute('tabindex')).toBe('0');
	});
	it.each([false, true])('keeps the cell range when pending marks update, focused=%s', async (focused) => {
		const { props, view } = render();
		const cell = await vi.waitFor(() => {
			const td = [...host.querySelectorAll('td')].find(el => el.textContent === 'Dana');
			if (!td) throw new Error('table not drawn');
			return td;
		});
		if (focused) { view.contentDOM.focus(); expect(document.activeElement).toBe(view.contentDOM); }
		const range = document.createRange();
		range.selectNodeContents(cell);
		document.getSelection()!.removeAllRanges();
		document.getSelection()!.addRange(range);
		expect(document.getSelection()!.toString()).toBe('Dana');
		const doc = new Y.Doc();
		const text = doc.getText('text');
		text.insert(0, DOC);
		props.pendingComment = selectComment({ doc, text } as LiveDocumentSession, DOC.indexOf('Dana'), DOC.indexOf('Dana') + 4);
		flushSync();
		await vi.waitFor(() => expect(cell.querySelector('.cm-comment-pending')).not.toBeNull());
		expect(document.getSelection()!.toString()).toBe('Dana');
		expect(host.querySelector('.cm-md-cell-editor')).toBeNull();
		expect(view.state.doc.toString()).toBe(DOC);
		doc.destroy();
	});
});
