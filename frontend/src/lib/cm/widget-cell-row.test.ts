import { describe, expect, it } from 'vitest';
import { Lexer, type Tokens } from 'marked';
import { tableCells } from './widget-cells';
import { contentOffset, normalizeContent, readCell, sourceOffset, unescapeCell, writeCell, writeCellEdit } from './widget-cell-row';

/**
 * The row mapping, checked against the repo's own table parser (`tableCells`) and against GFM as `marked` (the app's renderer)
 * reads it: whatever content is written into whatever cell of whatever row, the row keeps every other cell as it was, the
 * written cell reads back as the content stored, and both parsers agree on the row.
 */

/** A small deterministic generator, so a failure names the seed that reproduces it. */
function random(seed: number) {
	let state = Math.imul(seed, 2654435761) >>> 0 || 1;
	const next = () => { state ^= state << 13; state >>>= 0; state ^= state >>> 17; state ^= state << 5; state >>>= 0; return state / 0x100000000; };
	for (let i = 0; i < 8; i++) next(); // a small seed's first values are small too
	return { next, pick: <T>(items: readonly T[]) => items[Math.floor(next() * items.length)], int: (n: number) => Math.floor(next() * n) };
}

const ALPHABET = ['a', 'b', ' ', '\\', '\\', '|', '`', '*', 'x', '\n'] as const;
const text = (g: ReturnType<typeof random>, max: number) => Array.from({ length: g.int(max + 1) }, () => g.pick(ALPHABET)).join('');

/** A content a cell can hold (what reading a cell gives): no line break, every pipe after an even backslash run, no trailing odd run. */
const content = (g: ReturnType<typeof random>, max = 6) => normalizeContent('', text(g, max).replace(/\n/g, '')).text;

type Style = { lead: boolean; trail: boolean; pad: boolean };
/** A row of `cells` contents in one of the written styles: compact or padded, with or without its outer pipes. */
function rowOf(cells: string[], style: Style) {
	const parts = cells.map((cell) => {
		const source = cell.replaceAll('|', '\\|');
		return style.pad ? ` ${source} ` : source;
	});
	return `${style.lead ? '|' : ''}${parts.join('|')}${style.trail ? '|' : ''}`;
}

/** The cells of a row as GFM reads it, under a header of `columns` columns. */
function gfmCells(row: string, columns: number): string[] | null {
	const head = `|${Array.from({ length: columns }, (_, i) => ` h${i} `).join('|')}|`;
	const src = `${head}\n|${' --- |'.repeat(columns)}\n${row}\n`;
	const token = Lexer.lex(src).find((item) => item.type === 'table') as Tokens.Table | undefined;
	if (!token || token.rows.length !== 1) return null;
	return token.rows[0].map((cell) => cell.text);
}

/** The cells of a row as the repo reads them: their content, trimmed as drawn. */
const repoCells = (row: string) => tableCells(row).map((span) => unescapeCell(row.slice(span.rawFrom, span.rawTo)).trim());

describe('writeCell against tableCells and GFM', () => {
	it('keeps every row a row of its cells, each reading back as written, for random contents, rows and edits', () => {
		let ran = 0, padded = 0, escaped = 0;
		for (let seed = 1; seed <= 4000; seed++) {
			const g = random(seed);
			const columns = 1 + g.int(4);
			const real = Math.max(1, columns - g.int(3)); // a row may be short: its last cells padded in
			const cells = Array.from({ length: real }, () => content(g));
			const style: Style = { lead: g.next() < 0.8, trail: g.next() < 0.8, pad: g.next() < 0.5 };
			// A row needs a pipe; a single cell without both outer pipes is not a row a table can hold, nor can a blank edge cell.
			if (!style.lead && !style.trail && real === 1) style.lead = true;
			if (!style.lead && !cells[0].trim()) style.lead = true;
			if (!style.trail && !cells[real - 1].trim()) style.trail = true;
			const row = rowOf(cells, style);
			const before = gfmCells(row, columns);
			if (!before) continue; // the generator made something GFM does not read as one row (a block start without a pipe)
			const col = g.int(columns);
			const typed = text(g, 8);
			const old = readCell(row, col);
			ran++;
			if (col >= real) padded++;
			if (/\\(\||$)/.test(typed)) escaped++;
			const written = writeCell(row, col, typed);
			const name = `seed ${seed}: ${JSON.stringify(typed)} into column ${col} of ${JSON.stringify(row)} gives ${JSON.stringify(written)}`;
			expect(written.includes('\n'), name).toBe(false);
			const stored = normalizeContent(old, typed).text;
			const expectedCells = Math.max(real, stored ? col + 1 : real);
			expect(tableCells(written).length, `${name}: the repo parser's cell count`).toBe(expectedCells);
			const read = readCell(written, col);
			if (col < real || stored) expect(read.trim(), `${name}: the cell reads back as stored`).toBe(stored.trim());
			if (col < real) expect(read, `${name}: byte for byte`).toBe(stored);
			const repo = repoCells(written);
			for (let other = 0; other < real; other++) {
				if (other !== col) expect(repo[other], `${name}: column ${other} is untouched`).toBe(cells[other].trim());
			}
			const gfm = gfmCells(written, columns);
			expect(gfm, `${name}: GFM still reads one row`).not.toBeNull();
			for (let at = 0; at < columns; at++) expect(gfm![at], `${name}: GFM agrees on column ${at}`).toBe(repo[at] ?? '');
			// Writing back what was read changes nothing.
			expect(writeCell(written, col, read), `${name}: idempotent`).toBe(written);
		}
		// The cases the generator is for are actually run: padded columns, and backslashes before a pipe or at the end.
		expect(ran).toBeGreaterThan(3000);
		expect(padded).toBeGreaterThan(300);
		expect(escaped).toBeGreaterThan(300);
	});

	it('sends the minimal changes, which turn the old row into the new one', () => {
		for (let seed = 1; seed <= 2000; seed++) {
			const g = random(seed);
			const cells = [content(g), content(g), content(g)];
			const row = rowOf(cells, { lead: true, trail: true, pad: g.next() < 0.5 });
			const col = g.int(4);
			const typed = text(g, 8);
			const { row: written, changes } = writeCellEdit(row, col, typed);
			let applied = row;
			for (const change of [...changes].reverse()) applied = applied.slice(0, change.from) + change.insert + applied.slice(change.to);
			expect(applied, `seed ${seed}`).toBe(written);
		}
	});
});

describe('normalizing what a cell cannot hold as typed', () => {
	it('escapes a typed pipe, and keeps a pipe behind a typed backslash a pipe, with that backslash drawn', () => {
		expect(writeCell('| Login | Done |', 0, ' Login|no ')).toBe('| Login\\|no | Done |');
		// A backslash typed in front of a pipe: the content `a\|b` would draw `a|b`, so it is kept as a literal backslash.
		expect(normalizeContent('a|b', 'a\\|b').text).toBe('a\\\\|b');
		expect(writeCell('| a\\|b |', 0, 'a\\|b')).toBe('|a\\\\\\|b|');
	});

	it('removes a literal backslash in front of a pipe when it is deleted, instead of putting it back', () => {
		expect(normalizeContent(' a\\\\|b ', ' a\\|b ').text, 'Backspace or Delete of either source backslash').toBe(' a|b ');
		expect(writeCell('| a\\\\\\|b | Done |', 0, ' a\\|b ')).toBe('| a\\|b | Done |');
	});

	it('keeps a trailing backslash off the closing pipe, in a compact row and once a pad is deleted', () => {
		expect(writeCell('|Login|Done|', 0, 'Login\\')).toBe('|Login\\ |Done|');
		expect(writeCell('| Login\\ | Done |', 0, ' Login\\')).toBe('| Login\\ | Done |');
		expect(writeCell('|Login|Done|', 0, 'Login\\\\'), 'an even run escapes nothing').toBe('|Login\\\\|Done|');
	});

	it('turns line breaks into a space between words, and into nothing beside a space', () => {
		expect(normalizeContent(' Login ', ' Logintwo\nlines ').text).toBe(' Logintwo lines ');
		expect(normalizeContent(' Login ', ' Login\n ').text).toBe(' Login ');
		expect(normalizeContent('', '\n').text).toBe('');
	});

	it('gives a row without outer pipes the pipe its edge cell needs', () => {
		expect(writeCell('a | b', 0, '')).toBe('|| b');
		expect(writeCell('a | b', 1, ' ')).toBe('a | |');
		expect(writeCell('a | b', 0, '# x ')).toBe('|# x | b');
	});

	it('opens a column the row lacks with the empty cells before it', () => {
		expect(writeCell('| Login |', 2, 'Z')).toBe('| Login | | Z |');
		expect(writeCell('| Login', 1, 'Z')).toBe('| Login | Z |');
		expect(writeCell('| Login |', 2, ''), 'nothing typed opens nothing').toBe('| Login |');
	});
});

describe('where places in a cell sit', () => {
	it('maps content and source offsets through escaped pipes both ways', () => {
		const source = ' a\\|b\\\\\\|c ', cellContent = unescapeCell(source);
		expect(cellContent).toBe(' a|b\\\\|c ');
		for (let at = 0; at <= cellContent.length; at++) expect(contentOffset(source, sourceOffset(cellContent, at)), `content ${at}`).toBe(at);
		expect(sourceOffset(cellContent, 2), 'before a pipe is before its escape').toBe(2);
		expect(contentOffset(source, 3), 'between an escape and its pipe is before the pipe').toBe(2);
	});

	it('keeps the caret where the reader typed through what normalizing adds or takes away', () => {
		expect(normalizeContent('a|b', 'a\\|b').at(2), 'after the typed backslash, before the pipe').toBe(3);
		expect(normalizeContent('Login', 'Login\\').at(6), 'before the space a trailing backslash gains').toBe(6);
		expect(normalizeContent(' a\\\\|b ', ' a\\|b ').at(3), 'where the deleted backslash was').toBe(2);
		expect(writeCellEdit('| Login |', 1, 'Z').at(1), 'after the typed character, inside the new pads').toBe(2);
	});

	it('sends the edit itself when its text matches what is beside it', () => {
		const { changes } = writeCellEdit('| aa |', 0, ' aaa ', { from: 2, to: 2 });
		expect(changes, 'after the first `a` (offset 2 of the cell), not after the second').toEqual([{ from: 3, to: 3, insert: 'a' }]);
	});
});
