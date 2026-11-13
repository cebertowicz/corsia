import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Lexer, type Tokens } from 'marked';
import { A as AUTHOR, type Rig } from './typed-reveal-rig';
import { tableCells } from './widget-cells';
import { openCellState } from './widget-cell-state';
import { cellEl, cleanup, clicked, build, cellText, deleted, key, press, table, typed } from './widget-cell-rig';

/**
 * What the row model guarantees end to end, through the cell's own editor: the row a cell is written into keeps its cells
 * whatever the cell holds; the drawn table follows the source's own cells; and the open cell stays the one the reader opened
 * when other rows change.
 */

const rigs: Rig[] = [];
const track = <T extends Rig>(r: T) => { rigs.push(r); return r; };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; vi.useRealTimers(); });

const docWith = (head: string, rows: string[]) => `# Plan\n\n${head}\n${head.replace(/[^|]+/g, ' --- ')}\n${rows.join('\n')}\n\nafter\n`;

/** The table's source lines in the document, as the repo's parser and as GFM (marked) split them. */
function tableSource(r: Rig, head: string) {
	const lines = r.view.state.doc.toString().split('\n');
	const start = lines.indexOf(head);
	let end = start;
	while (end < lines.length && lines[end].trim() !== '') end++;
	return lines.slice(start, end).join('\n');
}
function rowCells(r: Rig, head: string, row: number) {
	const src = tableSource(r, head);
	return tableCells(src).filter((span) => span.row === row).map((span) => src.slice(span.from, span.to));
}
function gfmRow(r: Rig, head: string, row: number) {
	const token = Lexer.lex(tableSource(r, head))[0] as Tokens.Table;
	expect(token.type, 'GFM still reads a table').toBe('table');
	return (row === 0 ? token.header : token.rows[row - 1]).map((cell) => cell.text);
}

describe('(a) a trailing backslash never escapes the closing pipe', () => {
	it('in a compact row, a backslash typed at the end of a cell leaves the row its cells', () => {
		const head = '|Feature|Status|';
		const r = track(build(docWith(head, ['|Login|Done|'])));
		clicked(r);
		typed(r, 5, '\\');
		expect(rowCells(r, head, 1), 'the repo parser').toEqual(['Login\\', 'Done']);
		expect(gfmRow(r, head, 1), 'GFM').toEqual(['Login\\', 'Done']);
	});

	it('in a padded row, a space typed after a trailing backslash and deleted again leaves the row its cells', () => {
		const head = '| Feature | Status |';
		const r = track(build(docWith(head, ['| Login | Done |'])));
		clicked(r);
		typed(r, 5, '\\');
		typed(r, 6, ' ');
		deleted(r, 6, 7);
		expect(cellText(r), 'setup: the cell holds the backslash last').toBe('Login\\');
		expect(rowCells(r, head, 1)).toEqual(['Login\\', 'Done']);
		expect(gfmRow(r, head, 1)).toEqual(['Login\\', 'Done']);
	});
});

describe('(b) the drawn table follows the source cells a row loses', () => {
	it('undoing text typed into a padded cell empties the drawn cell, open and after it closes', () => {
		const head = '| Feature | Status | Note |';
		const r = track(build(docWith(head, ['| Login |'])));
		clicked(r, cellEl(r, 1, 2));
		typed(r, 0, 'Z');
		expect(r.view.state.doc.toString(), 'setup: the row gained its cells').toContain('| Login |  | Z |');
		expect(table(r)!.rows[1].cells[2].textContent).toBe('Z');
		key(r, 'z');
		expect(r.view.state.doc.toString(), 'setup: undo took the cells out').toContain('| Login |\n');
		expect(table(r)!.rows[1].cells[2].textContent, 'the drawn cell under the open editor').toBe('');
		press(r, 'Escape');
		expect(table(r)!.rows[1].cells[2].textContent, 'the drawn cell once closed').toBe('');
	});

	it("an agent's live edit that drops a row's last cell redraws it empty", () => {
		const head = '| Feature | Status |';
		const r = track(build(docWith(head, ['| Login | Done |', '| Sync | Open |'])));
		clicked(r, cellEl(r, 2, 0));
		r.edit(AUTHOR, (text) => {
			const at = text.toString().indexOf(' Done |');
			text.delete(at, ' Done |'.length);
		});
		expect(r.view.state.doc.toString(), 'setup: the row lost its cell').toContain('| Login |\n');
		expect(table(r)!.rows[1].cells[1].textContent).toBe('');
	});
});

describe('(c) the open cell is its row and its column', () => {
	it('an open padded cell stays in its column when a row is inserted above it', () => {
		const head = '| Feature | Status | Note |';
		const r = track(build(docWith(head, ['| Login |'])));
		clicked(r, cellEl(r, 1, 2));
		expect(r.view.state.field(openCellState), 'setup: the padded third cell is open').toMatchObject({ row: 1, col: 2, padded: true });
		r.edit(AUTHOR, (text) => text.insert(text.toString().indexOf('| Login |'), '| New | x | y |\n'));
		expect(r.view.state.field(openCellState), 'the same cell, one row down').toMatchObject({ row: 2, col: 2, padded: true });
		typed(r, 0, 'Z');
		expect(r.view.state.doc.toString(), 'the text lands in the third column').toContain('| Login |  | Z |');
		expect(cellText(r)).toBe('Z');
	});
});
