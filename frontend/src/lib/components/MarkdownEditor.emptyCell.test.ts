import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import MarkdownEditor from './MarkdownEditor.svelte';
import { closeCell } from '$lib/cm/widget-cell-state';

/**
 * An empty cell has no text to select for a comment, so while its editor is open and still empty the editor's own selection
 * flow offers Comment for the whole cell. The first character typed takes the offer away, and emptying the cell again brings
 * it back. The editor's selection flow is that offer's one writer.
 */

const ROW = '| Login |  |';
const DOC = `# Plan\n\n| Feature | Status |\n| --- | --- |\n${ROW}\n\nafter\n`;
const rowFrom = DOC.indexOf(ROW), rowTo = rowFrom + ROW.length;

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
	vi.restoreAllMocks();
	host?.remove();
	const layout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	delete layout.getClientRects;
	delete layout.getBoundingClientRect;
});

async function openCell(col: number) {
	const onSelect = vi.fn();
	host = document.body.appendChild(document.createElement('div'));
	component = mount(MarkdownEditor, { target: host, props: { value: DOC, onSelect } });
	flushSync();
	const td = await vi.waitFor(() => {
		const cell = host.querySelector('.cm-md-table table')?.querySelectorAll('tr')[1]?.cells[col];
		if (!cell) throw new Error('table not drawn');
		return cell;
	});
	td.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
	const el = host.querySelector<HTMLElement>('.cm-md-cell-editor .cm-editor');
	expect(el, 'setup: the cell editor opened').not.toBeNull();
	const cell = EditorView.findFromDOM(el!)!;
	const edit = (from: number, to: number, insert: string) =>
		cell.dispatch({ changes: { from, to, insert }, selection: { anchor: from + insert.length }, userEvent: insert ? 'input.type' : 'delete.content.back' });
	const doc = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	return { onSelect, edit, close: () => doc.dispatch({ effects: closeCell.of(null) }) };
}

const wholeEmptyCell = () => expect.objectContaining({ inWidget: true, from: expect.any(Number), to: expect.any(Number) });
const last = (onSelect: ReturnType<typeof vi.fn>) => onSelect.mock.calls.at(-1)?.[0];

describe('the Comment offer on an empty cell being edited', () => {
	it('is raised for the whole cell when the editor opens empty', async () => {
		const { onSelect } = await openCell(1);
		const offer = last(onSelect);
		expect(offer, 'the offer is up as soon as the empty cell opens').toEqual(wholeEmptyCell());
		expect(offer.from >= rowFrom && offer.to <= rowTo, 'anchored on that cell, in its own row').toBe(true);
	});

	it('goes when the first character is typed, and comes back when everything in the cell is deleted', async () => {
		const { onSelect, edit } = await openCell(1);
		edit(1, 1, 'x');
		expect(last(onSelect), 'typing takes the offer away').toBeNull();
		edit(1, 2, '');
		expect(last(onSelect), 'the cell is empty again, so the offer is back').toEqual(wholeEmptyCell());
	});

	it('stays up when the cell closes, so a press on it (which closes the cell) still starts the comment', async () => {
		const { onSelect, close } = await openCell(1);
		close();
		expect(last(onSelect), 'closing the empty cell does not take the offer away').toEqual(wholeEmptyCell());
	});

	it('is not raised when the cell opened has text', async () => {
		const { onSelect } = await openCell(0);
		expect(onSelect.mock.calls.map(([offer]) => offer).filter(Boolean), 'a caret in text raises nothing').toEqual([]);
	});
});
