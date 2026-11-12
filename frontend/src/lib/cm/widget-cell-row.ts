import { tableCells } from './widget-cells';

/**
 * The one mapping between a table cell's content and its row's source.
 *
 * A cell's *content* is its source between its pipes, padding included, with the table's own `\|` escapes taken out: what
 * GFM hands the inline parser for that cell, and what the cell's editor holds and draws. Reading it back is `tableCells`
 * (the one table-source parser) plus that unescape. Writing it is `writeCell`, the only writer of a cell's source, so
 * whether the row stays well formed is decided here, from the whole row, and never by patching an edit after the fact:
 *
 * - every `|` in the content is written with an odd run of backslashes before it, so it stays text;
 * - a trailing odd run of backslashes is kept off the closing pipe by a space, so it escapes nothing;
 * - a row with no leading (or trailing) pipe gets one when its first (or last) cell is written, so a cell left blank or
 *   starting with block syntax neither drops out of the row nor ends the table;
 * - a column the row does not have yet (a cell the renderer pads in) is written with the delimiters of every missing cell
 *   before it, empty.
 *
 * Content that cannot be written as typed is normalised, and only here (`normalizeContent`): line breaks become a space,
 * and a pipe left behind an odd run of backslashes takes one more, or, where that would only restore the content the
 * cell had (a backslash in front of a pipe was deleted), one fewer. Both render the same.
 */

/** A cell's content from its source between its pipes: the table's own `\|` escapes come out, as GFM takes them out. */
export const unescapeCell = (source: string) => source.replaceAll('\\|', '|');

/** A normalised content's source between its pipes: every pipe in it has an even backslash run, so each takes one more. */
const escapeCell = (content: string) => content.replaceAll('|', '\\|');

/** Where a content offset sits in that content's source: each pipe before it carries the escape written in front of it. */
export function sourceOffset(content: string, at: number): number {
	let offset = at;
	for (let i = 0; i < at && i < content.length; i++) if (content[i] === '|') offset++;
	return offset;
}

/** Where a source offset sits in the cell's content: a table escape is not there, and a place between it and its pipe is before the pipe. */
export function contentOffset(source: string, at: number): number {
	let offset = 0;
	for (let i = 0; i < at && i < source.length; i++) if (!(source[i] === '\\' && source[i + 1] === '|')) offset++;
	return offset;
}

/** A cell's content in a row, by column; a column the row does not have reads as empty. */
export function readCell(row: string, col: number): string {
	const span = tableCells(row).find((cell) => cell.col === col);
	return span ? unescapeCell(row.slice(span.rawFrom, span.rawTo)) : '';
}

/** Content as it will be stored, and where each place in the typed content lands in it. */
export interface Normalized { text: string; at: (offset: number) => number }

const BREAK = /[\r\n]/;

function normalizeWith(typed: string, pipeRun: 'up' | 'down'): Normalized & { odd: boolean } {
	let text = '', run = 0, odd = false;
	const map: number[] = [];
	for (let i = 0; i < typed.length; i++) {
		const char = typed[i];
		if (BREAK.test(char)) {
			map[i] = text.length;
			// A run of breaks is one space between words; beside a space, or at either end, it is nothing.
			const next = typed.slice(i).search(/[^\r\n]/);
			const after = next < 0 ? '' : typed[i + next];
			if (!BREAK.test(typed[i - 1] ?? '') && text && !/\s$/.test(text) && after && !/\s/.test(after)) text += ' ';
			run = 0;
			continue;
		}
		if (char === '|' && run % 2 === 1) {
			odd = true;
			if (pipeRun === 'up') {
				// One more backslash, in front of the pipe: the place before the pipe stays before it.
				map[i] = text.length + 1;
				text += '\\|';
			} else {
				// One fewer: the backslash right before the pipe goes, and both places beside it are where it was.
				text = text.slice(0, -1);
				map[i - 1] = map[i] = text.length;
				text += '|';
			}
			run = 0;
			continue;
		}
		map[i] = text.length;
		text += char;
		run = char === '\\' ? run + 1 : 0;
	}
	map[typed.length] = text.length;
	let tail = 0;
	while (text[text.length - 1 - tail] === '\\') tail++;
	// A trailing odd run would escape the closing pipe; a space keeps it off. The place at the end stays before the space.
	if (tail % 2 === 1) text += ' ';
	return { text, odd, at: (offset) => map[Math.max(0, Math.min(offset, typed.length))] };
}

/**
 * `typed` as the cell will store it, given the content `old` it had. A pipe behind an odd backslash run takes one more
 * backslash (a backslash typed in front of a pipe stays, drawn), unless that is exactly the content the cell had, which
 * means a backslash in front of the pipe was just deleted: then it takes one fewer, and the deletion stands.
 */
export function normalizeContent(old: string, typed: string): Normalized {
	const up = normalizeWith(typed, 'up');
	return up.odd && up.text === old ? normalizeWith(typed, 'down') : up;
}

type Change = { from: number; to: number; insert: string };

/** The smallest change from one run of text to the next, keeping at most `keep` shared leading and `tail` shared trailing characters. */
export function runDifference(oldText: string, newText: string, at = 0, maxKeep = Infinity, maxTail = Infinity): Change {
	let keep = 0;
	while (keep < maxKeep && keep < oldText.length && keep < newText.length && oldText[keep] === newText[keep]) keep++;
	let tail = 0;
	while (tail < maxTail && tail < oldText.length - keep && tail < newText.length - keep && oldText[oldText.length - 1 - tail] === newText[newText.length - 1 - tail]) tail++;
	return { from: at + keep, to: at + oldText.length - tail, insert: newText.slice(keep, newText.length - tail) };
}

/** One cell written into its row: the row after it, the minimal changes from the row before, and where typed places land. */
export interface CellWrite {
	row: string;
	changes: Change[];
	/** Where a place in the typed content lands in the content now stored. */
	at: (offset: number) => number;
}

/**
 * Writes `typed` as the content of column `col` of `row` (one source line). `edited` is the range of the old content the
 * edit touched, so the change sent is the edit itself and not an equal-looking one beside it (`a` typed after the first of
 * two `a`s lands after the first). A column the row lacks opens with one space each side, as a written cell is padded.
 */
export function writeCellEdit(row: string, col: number, typed: string, edited?: { from: number; to: number }): CellWrite {
	const spans = tableCells(row), span = spans.find((cell) => cell.col === col);
	if (!span) {
		const last = spans[spans.length - 1];
		const normalized = normalizeContent('', typed);
		if (!normalized.text) return { row, changes: [], at: () => 0 };
		// The row's last cell needs a pipe after it, then every missing cell before this one is opened, empty. A space before
		// that pipe pads the cell and keeps a backslash it ends with from escaping it.
		const close = last && last.rawTo === row.length ? (/\s$/.test(row) ? '|' : ' |') : '';
		const insert = `${close}${' |'.repeat(Math.max(0, col - spans.length))} ${escapeCell(normalized.text)} |`;
		return { row: row + insert, changes: [{ from: row.length, to: row.length, insert }], at: (offset) => normalized.at(offset) + 1 };
	}
	const oldSource = row.slice(span.rawFrom, span.rawTo), old = unescapeCell(oldSource);
	const normalized = normalizeContent(old, typed), source = escapeCell(normalized.text);
	const changes: Change[] = [];
	// A cell running from the line's start or to its end has no pipe on that side, so the row gains one.
	if (span.rawFrom === 0) changes.push({ from: 0, to: 0, insert: '|' });
	const keep = edited ? sourceOffset(old, edited.from) : Infinity;
	const tail = edited ? oldSource.length - sourceOffset(old, edited.to) : Infinity;
	const cell = runDifference(oldSource, source, span.rawFrom, keep, tail);
	if (cell.from !== cell.to || cell.insert) changes.push(cell);
	if (span.rawTo === row.length) changes.push({ from: row.length, to: row.length, insert: '|' });
	const written = `${span.rawFrom === 0 ? '|' : ''}${row.slice(0, span.rawFrom)}${source}${row.slice(span.rawTo)}${span.rawTo === row.length ? '|' : ''}`;
	if (written === row) return { row, changes: [], at: normalized.at };
	return { row: written, changes: merge(changes), at: normalized.at };
}

/** `writeCellEdit`'s row alone: the pure mapping from a row, a column and its new content to the row's new source. */
export const writeCell = (row: string, col: number, content: string) => writeCellEdit(row, col, content).row;

/** Changes that touch end to start become one, as `ChangeSet.of` takes them. */
function merge(changes: Change[]): Change[] {
	const out: Change[] = [];
	for (const change of changes) {
		const last = out[out.length - 1];
		if (last && last.to === change.from) out[out.length - 1] = { from: last.from, to: change.to, insert: last.insert + change.insert };
		else out.push(change);
	}
	return out;
}
