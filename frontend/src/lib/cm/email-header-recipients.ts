import type { EditorState } from '@codemirror/state';

// Reading the To and Cc lines of an email draft header: where they are, and which entries are added or not full addresses.

export const FIELD = /^(from|to|cc|subject|in reply to|note)\s*:/i;

/** Where the To and Cc lines are in the header. */
export function recipientLines(state: EditorState) {
	let to: { number: number; to: number } | null = null;
	let cc: { number: number; to: number } | null = null;
	const { doc } = state;
	for (let n = 1; n <= doc.lines; n++) {
		const line = doc.line(n);
		if (line.text === '') break;
		const match = FIELD.exec(line.text);
		const key = match ? match[1].toLowerCase() : '';
		if (key === 'to' && !to) to = { number: n, to: line.to };
		if (key === 'cc' && !cc) cc = { number: n, to: line.to };
	}
	return { to, cc };
}

/** A complete address: something@something.tld, so one still being typed is not marked. */
const COMPLETE_ADDRESS = /^[^@\s<>,]+@[^@\s<>,]+\.[^@\s<>,]+$/;

/** The entries of a To or Cc value (name and address, or whatever was typed), each with its range in the document. */
function recipientEntryRanges(value: string, valueFrom: number): Array<{ from: number; to: number; text: string }> {
	const found: Array<{ from: number; to: number; text: string }> = [];
	let at = 0;
	for (const part of value.split(',')) {
		const lead = part.length - part.trimStart().length;
		const text = part.trim();
		if (text) found.push({ from: valueFrom + at + lead, to: valueFrom + at + lead + text.length, text });
		at += part.length + 1;
	}
	return found;
}

/** The addresses of a To or Cc value that are not in `known`, each with the range of its entry (name and address) in the document. */
export function addedRecipients(value: string, valueFrom: number, known: ReadonlySet<string>): Array<{ from: number; to: number }> {
	return recipientEntryRanges(value, valueFrom).filter(({ text }) => {
		const bracket = /<([^>]*)>\s*$/.exec(text);
		const address = (bracket ? bracket[1] : text).trim();
		return COMPLETE_ADDRESS.test(address) && !known.has(address.toLowerCase());
	}).map(({ from, to }) => ({ from, to }));
}

/** The entries of a To or Cc value that are among `bad` (the safeguard's not-a-full-address entries), as typed. */
export function badRecipientRanges(value: string, valueFrom: number, bad: ReadonlySet<string>): Array<{ from: number; to: number }> {
	return recipientEntryRanges(value, valueFrom).filter(({ text }) => bad.has(text)).map(({ from, to }) => ({ from, to }));
}
