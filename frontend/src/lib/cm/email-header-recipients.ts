import type { EditorState } from '@codemirror/state';
import { isFullAddress } from '$lib/emailDraft';

// Reading the To and Cc lines of an email draft header: where they are, and which entries are added or not full addresses.

export const FIELD = /^(from|to|cc|subject|note)\s*:/i;

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

/** The entries of a To or Cc value as typed, each with its range in the document. */
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

/** The addresses of a To or Cc value that are not in `known`, each with its range in the document. */
export function addedRecipients(value: string, valueFrom: number, known: ReadonlySet<string>): Array<{ from: number; to: number }> {
	return recipientEntryRanges(value, valueFrom)
		.filter(({ text }) => isFullAddress(text) && COMPLETE_ADDRESS.test(text) && !known.has(text.toLowerCase()))
		.map(({ from, to }) => ({ from, to }));
}

/** The entries of a To or Cc value the send would refuse (`isFullAddress`), each with its range in the document. */
export function badRecipientRanges(value: string, valueFrom: number): Array<{ from: number; to: number }> {
	return recipientEntryRanges(value, valueFrom).filter(({ text }) => !isFullAddress(text)).map(({ from, to }) => ({ from, to }));
}
