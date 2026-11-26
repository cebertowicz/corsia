// Where a kept comment draft's passage is: its quote with the text on either side of it, so a draft that comes back (an
// open, a reload) opens on its own words and is sent there, and never on a copy of them elsewhere. The server places a
// resent comment by the same rule (`kept_place` in backend/doceditor/quote_anchor.py); this places its box.
import type { ChatCommentAnchor } from '$lib/chatCommentAnchors';

/** A passage of a document's text: UTF-16 offsets, as the editor and the wire count them, and its quote's sides. */
export type KeptPlace = { from: number; to: number; quote: string; prefix: string; suffix: string };

/** Code points kept on each side, as the server's `anchor_context` keeps them; a side is shorter only at the text's start or end. */
const SIDE = 32;
const high = (unit: number) => unit >= 0xd800 && unit <= 0xdbff;
const low = (unit: number) => unit >= 0xdc00 && unit <= 0xdfff;

/** Up to 32 code points of `text` ending at UTF-16 offset `at`: a pair counts once, as Python counts it. */
function before(text: string, at: number): string {
	let start = at;
	for (let count = 0; count < SIDE && start > 0; count += 1) {
		start -= start >= 2 && low(text.charCodeAt(start - 1)) && high(text.charCodeAt(start - 2)) ? 2 : 1;
	}
	return text.slice(start, at);
}

/** Up to 32 code points of `text` starting at UTF-16 offset `at`. */
function after(text: string, at: number): string {
	let end = at;
	for (let count = 0; count < SIDE && end < text.length; count += 1) {
		end += end + 1 < text.length && high(text.charCodeAt(end)) && low(text.charCodeAt(end + 1)) ? 2 : 1;
	}
	return text.slice(at, end);
}

/** The passage `from`-`to` of `text` as a draft keeps it, with the sides the server compares. */
export function placeOf(text: string, from: number, to: number, quote = text.slice(from, to)): KeptPlace {
	return { from, to, quote, prefix: before(text, from), suffix: after(text, to) };
}

/** The kept sides a post carries (`anchor_prefix`, `anchor_suffix`), so the server places it by them when no capture is held for it; none with no place. */
export function keptSides(place: KeptPlace | null | undefined): { anchor_prefix?: string; anchor_suffix?: string } {
	return place ? { anchor_prefix: place.prefix, anchor_suffix: place.suffix } : {};
}

/** Every start of `place.quote` in `text` with both kept sides, overlapping copies included, as the server counts them. */
function matches(text: string, place: KeptPlace): number[] {
	const hits: number[] = [];
	if (!place.quote) return hits;
	for (let at = text.indexOf(place.quote); at !== -1; at = text.indexOf(place.quote, at + 1)) {
		if (before(text, at) === place.prefix && after(text, at + place.quote.length) === place.suffix) hits.push(at);
	}
	return hits;
}

/**
 * Where the kept passage is in `text` now: the one occurrence of its quote with both kept sides. None, or two or more,
 * is null: nothing in the text says which is right, so the draft shows with no place.
 */
export function findPlace(text: string, place: KeptPlace): [number, number] | null {
	const hits = matches(text, place);
	return hits.length === 1 ? [hits[0], hits[0] + place.quote.length] : null;
}

/**
 * The passage `from`-`to` as a draft keeps it, only when no other copy of its quote in `text` shares either side with it.
 * One edit cannot change both sides of another copy without touching that copy's own words, so a passage kept this way
 * is never found on other words after its own are cut. Otherwise null: the draft keeps its words, sends no place and
 * shows with no place.
 */
export function uniquePlaceOf(text: string, from: number, to: number, quote = text.slice(from, to)): KeptPlace | null {
	if (!quote || text.slice(from, to) !== quote) return null;
	const place = placeOf(text, from, to, quote);
	for (let at = text.indexOf(quote); at !== -1; at = text.indexOf(quote, at + 1)) {
		if (at !== from && (before(text, at) === place.prefix || after(text, at + quote.length) === place.suffix)) return null;
	}
	return place;
}

/**
 * Whether a chat passage is still at its kept offsets in its message's rendered text. A sent message does not change, so
 * the offsets are exact: the quote and its kept sides must read there as kept (a table's cells included, which the rendered
 * text joins with nothing between them). A repeat of the words elsewhere in the message is never taken for it.
 */
export function chatPlaceHolds(text: string, place: KeptPlace): boolean {
	return text.slice(place.from, place.to) === place.quote && before(text, place.from) === place.prefix && after(text, place.to) === place.suffix;
}

/** A chat passage as a press posts it: its quote with the whole of its message's text before and after it. */
export function chatAnchorAt(text: string, from: number, to: number): ChatCommentAnchor {
	return { from_off: from, to_off: to, quote: text.slice(from, to), anchor_prefix: text.slice(0, from), anchor_suffix: text.slice(to) };
}
