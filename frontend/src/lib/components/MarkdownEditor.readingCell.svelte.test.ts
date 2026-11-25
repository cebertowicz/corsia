import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import MarkdownEditor from './MarkdownEditor.svelte';
import { textlessCellSelection } from '$lib/cm/widget-cell-comment';
import { openCellState } from '$lib/cm/widget-cell-state';
import { selectionIn } from '$lib/cm/widget-selection';

const DOC = 'intro\n\n| Item | Status |\n|---|---|\n| Alpha |  |\n| Photo | ![photo](photo.png) |\n\nafter\n';
let host: HTMLElement, component: ReturnType<typeof mount> | undefined;
beforeEach(() => {
	Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
	Range.prototype.getBoundingClientRect = () => new DOMRect();
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined; host?.remove(); document.getSelection()?.removeAllRanges(); vi.restoreAllMocks();
});
async function render(reading: boolean, imageBase: { path: string; projectId: string | null } | null = null) {
	host = document.body.appendChild(document.createElement('div'));
	const onSelect = vi.fn();
	component = mount(MarkdownEditor, { target: host, props: { value: DOC, reading, onSelect, imageBase } }); flushSync();
	const table = await vi.waitFor(() => { const table = host.querySelector<HTMLElement>('.cm-md-table'); if (!table) throw new Error('no table'); return table; });
	const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	return { view, table, onSelect };
}
it('uses the same textless-cell helper for the existing open editor and a Reading drawn cell', async () => {
	let setup = await render(false);
	const empty = setup.table.querySelectorAll('tr')[1].cells[1]; empty.click();
	const old = textlessCellSelection(setup.view)!;
	expect(old).not.toBeNull();
	await unmount(component!); component = undefined; host.remove();
	setup = await render(true);
	const drawn = setup.table.querySelectorAll('tr')[1].cells[1];
	const reading = textlessCellSelection(setup.view, drawn)!;
	expect(reading).toMatchObject({ from: old.from, to: old.to, text: old.text });
	expect(setup.view.state.field(openCellState)).toBeNull();
	expect(host.querySelector('.cm-md-cell-editor')).toBeNull();
	const pointer = new MouseEvent('pointerdown', { bubbles: true, button: 0 });
	Object.defineProperty(pointer, 'pointerType', { value: 'touch' });
	drawn.dispatchEvent(pointer); drawn.click();
	expect(setup.onSelect).toHaveBeenCalledWith(expect.objectContaining({ from: old.from, to: old.to, text: old.text }));
	expect(setup.view.state.field(openCellState)).toBeNull();
});
const IMAGE_BASE = { path: '/ws/notes/brief.md', projectId: 'p1' };
it('offers Comment on a drawn image-only cell in Reading, as the open editor does, with no cell editor opened', async () => {
	let setup = await render(false, IMAGE_BASE);
	setup.table.querySelectorAll('tr')[2].cells[1].click();
	const old = textlessCellSelection(setup.view)!;
	expect(old, 'edit mode offers the cell its images').toMatchObject({ text: '![photo](photo.png)' });
	await unmount(component!); component = undefined; host.remove();
	setup = await render(true, IMAGE_BASE);
	const drawn = setup.table.querySelectorAll('tr')[2].cells[1];
	const reading = textlessCellSelection(setup.view, drawn)!;
	expect(reading).toMatchObject({ from: old.from, to: old.to, text: old.text, inWidget: true });
	expect(setup.view.state.field(openCellState)).toBeNull();
	expect(host.querySelector('.cm-md-cell-editor')).toBeNull();
	const pointer = new MouseEvent('pointerdown', { bubbles: true, button: 0 });
	Object.defineProperty(pointer, 'pointerType', { value: 'touch' });
	drawn.dispatchEvent(pointer); drawn.click();
	expect(setup.onSelect).toHaveBeenCalledWith(expect.objectContaining({ from: old.from, to: old.to, text: old.text }));
	expect(setup.view.state.field(openCellState)).toBeNull();
});
it('offers nothing on a drawn cell with text, and leaves nonempty native-range mapping alone', async () => {
	const { view, table } = await render(true, IMAGE_BASE);
	const rows = table.querySelectorAll('tr');
	expect(textlessCellSelection(view, rows[1].cells[0])).toBeNull();
	const range = document.createRange(); range.selectNodeContents(rows[1].cells[0]);
	const selected = selectionIn(view, table, range)!;
	expect(selected).toMatchObject({ from: DOC.indexOf('Alpha'), to: DOC.indexOf('Alpha') + 5, text: 'Alpha' });
	expect(view.state.doc.toString()).toBe(DOC);
});
