/** A headline split so each version can be kept on one line: `version` parts must never break inside themselves. */
export interface HeadlinePart {
	text: string;
	version: boolean;
}

// A version ends on a letter or digit, never on the sentence's full stop.
const VERSION = /\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?/g;

export function headlineParts(headline: string): HeadlinePart[] {
	const parts: HeadlinePart[] = [];
	let last = 0;
	for (const match of headline.matchAll(VERSION)) {
		if (match.index > last) parts.push({ text: headline.slice(last, match.index), version: false });
		parts.push({ text: match[0], version: true });
		last = match.index + match[0].length;
	}
	if (last < headline.length || parts.length === 0) parts.push({ text: headline.slice(last), version: false });
	return parts;
}
