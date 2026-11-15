/**
 * Keeps what the person typed when a phone's AutoFill suggestion (a name or phone number in the keyboard's strip)
 * replaces the field's whole value: the typed text is put back and the suggestion is inserted at the caret, the way the
 * chat input's contenteditable only ever inserts. The repair runs in a capture listener on the field, so `bind:value`
 * and the field's own `oninput` handlers only see the kept text.
 *
 * It repairs an edit only when all of these hold, so ordinary edits pass through untouched:
 * 1. a trusted `beforeinput` was seen for it (our own writes: the mention picker's synthetic `input`, a clear on send
 *    that fires no event at all, never qualify, and a stale snapshot can never resurrect cleared text);
 * 2. no IME composition is open;
 * 3. its `inputType` is `insertText`, `insertReplacementText` or empty (never a delete, undo/redo, paste, drop, line
 *    break or transpose);
 * 4. the field held text and the selection did not cover all of it (select-all then type replaces on purpose);
 * 5. it replaced the whole value, and that value held more than two words (a fix of some words, wherever the caret
 *    is, leaves the rest standing; in a field of one or two words a word fix looks the same, so it is left alone);
 * 6. the incoming text looks like a contact suggestion: one line, at most 120 characters (so a dictated paragraph or
 *    a whole-field rewrite is left alone).
 *
 * `prose` (default true) also gives the field the chat input's typing hints, unless the field sets them itself: no
 * autocomplete, autocorrect on, sentence capitals, spellcheck. Name and title fields pass `{ prose: false }` for the
 * repair alone.
 *
 * The inserted text is cut to fit the field's `maxlength`, as the browser cuts an insertion, since a value written from
 * script is not held to it.
 */
type TextField = HTMLTextAreaElement | HTMLInputElement;
type Snapshot = { value: string; start: number; end: number };

const PROSE_HINTS: [string, string][] = [['autocomplete', 'off'], ['autocorrect', 'on'], ['autocapitalize', 'sentences'], ['spellcheck', 'true']];
const REPAIRED_TYPES = new Set(['', 'insertText', 'insertReplacementText']);
const CONTACT_MAX = 120;

const blank = (char: string | undefined) => char !== undefined && /\s/.test(char);

/** Whether the browser, not our code, sent the event. A seam because a test can't make a trusted event. */
export const eventOrigin = { trusted: (event: Event) => event.isTrusted };

/** The value with the person's text kept and the incoming text at the caret, or null when the edit should stand. */
export function keptValue(prior: Snapshot, next: string, maxLength = -1): { value: string; caret: number } | null {
	const old = prior.value;
	if (!old.trim() || (prior.start <= 0 && prior.end >= old.length)) return null;
	const limit = Math.min(old.length, next.length);
	let head = 0;
	while (head < limit && old[head] === next[head]) head++;
	let tail = 0;
	while (tail < limit - head && old[old.length - 1 - tail] === next[next.length - 1 - tail]) tail++;
	// A shared prefix or suffix counts only up to a word boundary, so "Hello" replaced by "Hey Sam" brings the whole
	// "Hey Sam" back rather than "y Sam".
	while (head > 0 && !blank(old[head - 1])) head--;
	while (tail > 0 && !blank(old[old.length - tail])) tail--;
	const removedStart = head;
	const removedEnd = old.length - tail;
	if (removedEnd <= removedStart) return null;
	if (old.slice(0, removedStart).trim() || old.slice(removedEnd).trim()) return null;
	if (old.trim().split(/\s+/).length <= 2) return null;
	const incoming = next.slice(head, next.length - tail).trim();
	if (!incoming || incoming.length > CONTACT_MAX || /[\r\n]/.test(incoming)) return null;
	const before = old.slice(0, prior.start);
	const after = old.slice(prior.end);
	const lead = before && !blank(before[before.length - 1]) ? ' ' : '';
	const trail = after && !blank(after[0]) ? ' ' : '';
	const room = maxLength < 0 ? incoming.length : maxLength - before.length - lead.length - trail.length - after.length;
	const inserted = incoming.slice(0, Math.max(0, room)).trimEnd();
	if (!inserted) return { value: before + after, caret: before.length };
	const value = `${before}${lead}${inserted}${trail}${after}`;
	return { value, caret: before.length + lead.length + inserted.length };
}

export function keepTyped(node: TextField, options: { prose?: boolean } = {}) {
	if (options.prose !== false) {
		for (const [name, value] of PROSE_HINTS) if (!node.hasAttribute(name)) node.setAttribute(name, value);
	}
	let pending: Snapshot | null = null;
	let composing = false;
	const onBeforeInput = (event: Event) => {
		pending = eventOrigin.trusted(event) ? { value: node.value, start: node.selectionStart ?? 0, end: node.selectionEnd ?? 0 } : null;
	};
	const onInput = (event: Event) => {
		const prior = pending;
		pending = null;
		if (!prior || !eventOrigin.trusted(event) || composing) return;
		const input = event as InputEvent;
		if (input.isComposing || !REPAIRED_TYPES.has(input.inputType ?? '')) return;
		const kept = keptValue(prior, node.value, node.maxLength);
		if (!kept) return;
		node.value = kept.value;
		node.setSelectionRange(kept.caret, kept.caret);
	};
	const onCompositionStart = () => { composing = true; };
	const onCompositionEnd = () => { composing = false; };
	node.addEventListener('beforeinput', onBeforeInput, true);
	node.addEventListener('input', onInput, true);
	node.addEventListener('compositionstart', onCompositionStart);
	node.addEventListener('compositionend', onCompositionEnd);
	return {
		destroy() {
			node.removeEventListener('beforeinput', onBeforeInput, true);
			node.removeEventListener('input', onInput, true);
			node.removeEventListener('compositionstart', onCompositionStart);
			node.removeEventListener('compositionend', onCompositionEnd);
		},
	};
}
