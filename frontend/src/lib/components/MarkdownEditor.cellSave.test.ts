import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import MarkdownEditor from './MarkdownEditor.svelte';

/** The document's save key saves from inside an open table cell the same as from prose, with the cell's edit already saved in. */

const DOC = '# Plan\n\n| Feature | Status |\n| --- | --- |\n| Login | Done |\n\nafter\n';
const MOD = /Mac/.test(navigator.platform) ? 'metaKey' : 'ctrlKey';

let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;

beforeEach(() => {
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	layout.getClientRects = () => [] as unknown as DOMRectList;
	layout.getBoundingClientRect = () => new DOMRect();
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	delete layout.getClientRects;
	delete layout.getBoundingClientRect;
});

const save = (target: HTMLElement) => {
	const event = new KeyboardEvent('keydown', { key: 's', [MOD]: true, bubbles: true, cancelable: true });
	target.dispatchEvent(event);
	return event;
};

describe('Mod-s in an open table cell', () => {
	it('saves the document with the cell edit in it and keeps the browser from saving the page', async () => {
		const doc: string[] = [];
		host = document.body.appendChild(document.createElement('div'));
		const view = () => EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
		const onSave = vi.fn(() => doc.push(view().state.doc.toString()));
		component = mount(MarkdownEditor, { target: host, props: { value: DOC, onSave } });
		flushSync();
		const td = await vi.waitFor(() => {
			const cell = host.querySelector('.cm-md-table table')?.querySelectorAll('tr')[1]?.cells[0];
			if (!cell) throw new Error('table not drawn');
			return cell;
		});
		td.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
		const el = host.querySelector<HTMLElement>('.cm-md-cell-editor .cm-editor');
		expect(el, 'setup: the cell editor opened').not.toBeNull();
		const cell = EditorView.findFromDOM(el!)!;
		cell.dispatch({ changes: { from: 5, insert: 'x' }, selection: { anchor: 6 }, userEvent: 'input.type' });

		const event = save(cell.contentDOM);
		expect(onSave, 'the document saved once').toHaveBeenCalledTimes(1);
		expect(doc[0], 'with the cell edit already in the document').toContain('| Loginx | Done |');
		expect(event.defaultPrevented, 'the browser does not open its own save dialog').toBe(true);
	});
});
