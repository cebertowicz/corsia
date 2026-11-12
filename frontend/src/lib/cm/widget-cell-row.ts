import { tableCells } from './widget-cells';

/**
 * The one mapping between a table cell's content and its row's source.
 *
 * A cell's *content* is what GFM hands the inline parser for that cell: its source between its pipes with the table's own
 * `\|` escapes taken out and the pads around it trimmed. It is what the cell's editor holds. Reading it is `tableCells` (the
 * one table-source parser) plus `cellText`. Writing it is `serializeCell`, the one serializer, through `writeCellEdit`, the
 * only writer of a cell's source, so whether the row stays well formed is decided here, from the whole row:
 *
 * - the content is written between one space each side, so a backslash it ends with never escapes the closing pipe;
 * - every `|` in it is written with an odd run of backslashes before it, so it stays text: a backslash already in front of
 *   it is its escape, as in markdown, and otherwise one is added;
 * - line breaks become one space between words;
 * - a row with no leading (or trailing) pipe gets one when its first (or last) cell is written, so a cell left blank or
 *   starting with block syntax neither drops out of the row nor ends the table;
 * - a column the row does not have yet (a cell the renderer pads in) is written with every missing cell before it, empty.
 */

/** A cell's source with the table's own `\|` escapes taken out, as GFM takes them out. */
export const unescapeCell = (source: string) => source.replaceAll('\\|', '|');

/** Content whose every pipe is already a pipe the table reads as text, written as source: each pipe takes its escape. */
const escapeCell = (content: string) => content.replaceAll('|', '\\|');

const PADS = /^[ \t]+|[ \t]+$/g;

/** A cell's content from its source between its pipes: escapes out and pads trimmed, as GFM reads it. */
export const cellText = (source: string) => unescapeCell(source).replace(PADS, '');

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
	return span ? cellText(row.slice(span.rawFrom, span.rawTo)) : '';
}

/** A typed content as one cell's source between its pipes, the content that source reads back as, and where each typed place lands in it. */
export interface Serialized { source: string; text: string; at: (offset: number) => number }

const BREAK = /[\r\n]/;

/** The one serializer: what is written between a cell's pipes for content typed into it, an empty cell's included. */
export function serializeCell(typed: string): Serialized {
	let text = '', body = '', run = 0;
	const map: number[] = [];
	for (let i = 0; i < typed.length; i++) {
		const char = typed[i];
		if (BREAK.test(char)) {
			map[i] = text.length;
			// A run of breaks is one space between words; beside a space, or at either end, it is nothing.
			const next = typed.slice(i).search(/[^\r\n]/);
			const after = next < 0 ? '' : typed[i + next];
			if (!BREAK.test(typed[i - 1] ?? '') && text && !/\s$/.test(text) && after && !/\s/.test(after)) { text += ' '; body += ' '; }
			run = 0;
			continue;
		}
		if (char === '|' && run % 2 === 1) {
			// The backslash in front of the pipe is its escape, as in markdown: the pipe reads back as a plain pipe, and both
			// places beside that backslash are where the pipe now starts.
			text = text.slice(0, -1);
			map[i - 1] = map[i] = text.length;
			text += '|';
			body += '|';
			run = 0;
			continue;
		}
		map[i] = text.length;
		text += char;
		body += char === '|' ? '\\|' : char;
		run = char === '\\' ? run + 1 : 0;
	}
	map[typed.length] = text.length;
	return { source: ` ${body} `, text, at: (offset) => map[Math.max(0, Math.min(offset, typed.length))] };
}

/** The text an open cell's editor shows for the cell's source, and where that text starts in the source. */
export interface Held { text: string; base: number }

/**
 * What the open cell's editor holds for a cell's source: `held` (the text it already holds, or was just written from) while
 * the source still reads as it, so a space typed at either edge stays while typing goes on; otherwise the cell's content.
 */
export function heldText(source: string, held?: string): Held {
	const content = cellText(source);
	if (held !== undefined && held.replace(PADS, '') === content) {
		const base = source.indexOf(escapeCell(held));
		if (base >= 0) return { text: held, base: held ? base : Math.min(1, source.length) };
	}
	const lead = source.length - source.replace(/^[ \t]+/, '').length;
	return { text: content, base: content ? lead : Math.min(1, source.length) };
}

/** Where a place in the held text sits in the cell's source. */
export const heldToSource = (held: Held, at: number) => held.base + sourceOffset(held.text, at);

/** Where a place in the cell's source sits in the held text: a pad before it is its start, a pad after it its end. */
export const sourceToHeld = (source: string, held: Held, at: number) =>
	Math.min(held.text.length, contentOffset(source.slice(held.base), at - held.base));

type Change = { from: number; to: number; insert: string };

/** The smallest change from one run of text to the next, keeping at most `keep` shared leading and `tail` shared trailing characters. */
export function runDifference(oldText: string, newText: string, at = 0, maxKeep = Infinity, maxTail = Infinity): Change {
	let keep = 0;
	while (keep < maxKeep && keep < oldText.length && keep < newText.length && oldText[keep] === newText[keep]) keep++;
	let tail = 0;
	while (tail < maxTail && tail < oldText.length - keep && tail < newText.length - keep && oldText[oldText.length - 1 - tail] === newText[newText.length - 1 - tail]) tail++;
	return { from: at + keep, to: at + oldText.length - tail, insert: newText.slice(keep, newText.length - tail) };
}

/** One cell written into its row: the row after it, the minimal changes from the row before, and the content now stored. */
export interface CellWrite {
	row: string;
	changes: Change[];
	/** The content the written cell reads back as. */
	text: string;
	/** Where a place in the typed content lands in `text`. */
	at: (offset: number) => number;
}

/**
 * Writes `typed` as the content of column `col` of `row` (one source line), through `serializeCell`. `edited` is the range of
 * the cell's old source the edit touched, so the change sent is the edit itself and not an equal-looking one beside it (`a`
 * typed after the first of two `a`s lands after the first).
 */
export function writeCellEdit(row: string, col: number, typed: string, edited?: { from: number; to: number }): CellWrite {
	const spans = tableCells(row), span = spans.find((cell) => cell.col === col);
	const cell = serializeCell(typed);
	if (!span) {
		if (!cell.text) return { row, changes: [], text: '', at: () => 0 };
		const last = spans[spans.length - 1];
		// The row's last cell needs a pipe after it, then every missing cell before this one is opened, empty.
		const close = last && last.rawTo === row.length ? (/\s$/.test(row) ? '|' : ' |') : '';
		const insert = `${close}${`${serializeCell('').source}|`.repeat(Math.max(0, col - spans.length))}${cell.source}|`;
		return { row: row + insert, changes: [{ from: row.length, to: row.length, insert }], text: cell.text, at: cell.at };
	}
	const oldSource = row.slice(span.rawFrom, span.rawTo);
	const changes: Change[] = [];
	// A cell running from the line's start or to its end has no pipe on that side, so the row gains one.
	if (span.rawFrom === 0) changes.push({ from: 0, to: 0, insert: '|' });
	const keep = edited ? edited.from : Infinity;
	const tail = edited ? oldSource.length - edited.to : Infinity;
	const diff = runDifference(oldSource, cell.source, span.rawFrom, keep, tail);
	if (diff.from !== diff.to || diff.insert) changes.push(diff);
	if (span.rawTo === row.length) changes.push({ from: row.length, to: row.length, insert: '|' });
	const written = `${span.rawFrom === 0 ? '|' : ''}${row.slice(0, span.rawFrom)}${cell.source}${row.slice(span.rawTo)}${span.rawTo === row.length ? '|' : ''}`;
	if (written === row) return { row, changes: [], text: cell.text, at: cell.at };
	return { row: written, changes: merge(changes), text: cell.text, at: cell.at };
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
