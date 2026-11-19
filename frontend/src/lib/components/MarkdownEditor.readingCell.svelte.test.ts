import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import MarkdownEditor from './MarkdownEditor.svelte';
import { emptyCellSelection } from '$lib/cm/widget-cell-comment';
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
async function render(reading: boolean) {
	host = document.body.appendChild(document.createElement('div'));
	const onSelect = vi.fn();
	component = mount(MarkdownEditor, { target: host, props: { value: DOC, reading, onSelect } }); flushSync();
	const table = await vi.waitFor(() => { const table = host.querySelector<HTMLElement>('.cm-md-table'); if (!table) throw new Error('no table'); return table; });
	const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	return { view, table, onSelect };
}
it('uses the same empty-cell helper for the existing open editor and a Reading drawn cell', async () => {
	let setup = await render(false);
	const empty = setup.table.querySelectorAll('tr')[1].cells[1]; empty.click();
	const old = emptyCellSelection(setup.view)!;
	expect(old).not.toBeNull();
	await unmount(component!); component = undefined; host.remove();
	setup = await render(true);
	const drawn = setup.table.querySelectorAll('tr')[1].cells[1];
	const reading = emptyCellSelection(setup.view, drawn)!;
	expect(reading).toMatchObject({ from: old.from, to: old.to, text: old.text });
	expect(setup.view.state.field(openCellState)).toBeNull();
	expect(host.querySelector('.cm-md-cell-editor')).toBeNull();
	const pointer = new MouseEvent('pointerdown', { bubbles: true, button: 0 });
	Object.defineProperty(pointer, 'pointerType', { value: 'touch' });
	drawn.dispatchEvent(pointer); drawn.click();
	expect(setup.onSelect).toHaveBeenCalledWith(expect.objectContaining({ from: old.from, to: old.to, text: old.text }));
	expect(setup.view.state.field(openCellState)).toBeNull();
});
it('does not expand image-only cells or change nonempty native-range mapping', async () => {
	const { view, table } = await render(true);
	const rows = table.querySelectorAll('tr');
	expect(emptyCellSelection(view, rows[2].cells[1])).toBeNull();
	const range = document.createRange(); range.selectNodeContents(rows[1].cells[0]);
	const selected = selectionIn(view, table, range)!;
	expect(selected).toMatchObject({ from: DOC.indexOf('Alpha'), to: DOC.indexOf('Alpha') + 5, text: 'Alpha' });
	expect(view.state.doc.toString()).toBe(DOC);
});
