import { EditorState, StateField, Transaction, type Extension, type Range } from '@codemirror/state';
import { Decoration, EditorView, WidgetType, type DecorationSet } from '@codemirror/view';
import { cubicOut } from 'svelte/easing';
import { slide } from 'svelte/transition';
import { showAutolinkMarks } from './markdown-live';

// The compact header block of an email draft, drawn over the draft's own text: the header lines stay the document's
// text (editing them edits the draft), only their look changes. The lines are short and muted-labelled, a divider
// closes them, and the body below keeps the document's reading size. `Note:` lines are hidden here because the notes
// are drawn above the header (loud) and under it (quiet). The `From:` line is the sending account: shown, not editable.

const FIELD = /^(from|to|cc|subject|in reply to|note)\s*:/i;
/** Fields whose value is an address list (or a line being typed): at every width the value wraps, its continuation under the value column. */
const ADDRESS_FIELDS = new Set(['to', 'cc', 'in reply to']);

const CHEVRON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>';

const SLIDE_MS = 220;

/**
 * Opens or closes `list` with the transition the conversation rows use (`slide`, 220 ms, `cubicOut`), played as a Web Animation because
 * the list is plain DOM inside a widget. Under reduced motion it changes at once. `done` runs when the list is at rest.
 */
function slideList(list: HTMLElement, open: boolean, done?: () => void) {
	list.getAnimations().forEach((animation) => animation.cancel());
	if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { list.hidden = !open; done?.(); return; }
	list.hidden = false;
	const css = slide(list, { duration: SLIDE_MS, easing: cubicOut }).css!;
	const steps = Math.ceil(SLIDE_MS / (1000 / 60));
	const keyframes = Array.from({ length: steps + 1 }, (_, i) => {
		const eased = cubicOut(i / steps), t = open ? eased : 1 - eased;
		const frame: Record<string, string> = { offset: String(i / steps) };
		for (const declaration of css(t, 1 - t).split(';')) {
			const at = declaration.indexOf(':');
			if (at > 0) frame[declaration.slice(0, at).trim().replace(/-([a-z])/g, (_m, c: string) => c.toUpperCase())] = declaration.slice(at + 1).trim();
		}
		return frame;
	});
	const animation = list.animate(keyframes, { duration: SLIDE_MS });
	animation.onfinish = () => { list.hidden = !open; done?.(); };
	animation.oncancel = () => { /* a newer toggle took over */ };
}

/** The quiet notes: one collapsed line under the header that opens in place. */
class QuietNotesWidget extends WidgetType {
	constructor(readonly notes: readonly string[]) { super(); }

	eq(other: QuietNotesWidget) {
		return other.notes.length === this.notes.length && other.notes.every((note, index) => note === this.notes[index]);
	}

	toDOM(view: EditorView) {
		const root = document.createElement('div');
		root.className = 'cm-email-notes';
		const toggle = document.createElement('button');
		toggle.type = 'button';
		toggle.className = 'cm-email-notes-toggle';
		toggle.setAttribute('aria-expanded', 'false');
		const label = document.createElement('span');
		label.textContent = `${this.notes.length} ${this.notes.length === 1 ? 'note' : 'notes'}`;
		toggle.append(label);
		toggle.insertAdjacentHTML('beforeend', CHEVRON);
		const list = document.createElement('ul');
		list.className = 'cm-email-notes-list';
		list.hidden = true;
		for (const note of this.notes) {
			const item = document.createElement('li');
			item.textContent = note;
			list.append(item);
		}
		toggle.addEventListener('click', () => {
			const open = toggle.getAttribute('aria-expanded') !== 'true';
			toggle.setAttribute('aria-expanded', String(open));
			slideList(list, open, () => view.requestMeasure());
			// The editor re-measures while the height moves, so the text below follows it.
			const follow = () => { view.requestMeasure(); if (list.getAnimations().length) requestAnimationFrame(follow); };
			requestAnimationFrame(follow);
		});
		root.append(toggle, list);
		return root;
	}

	ignoreEvent() { return true; }
}

/** What the surface mounts into the attachments row; it returns the cleanup. Kept outside this file so the chips are the app's own. */
export type MountAttachments = (host: HTMLElement) => () => void;

/** The attachment chips, between the body and the signature divider. Rebuilt only when `key` (the attachments) changes. */
class AttachmentsWidget extends WidgetType {
	private static cleanup = new WeakMap<HTMLElement, () => void>();
	constructor(readonly key: string, readonly mountChips: MountAttachments) { super(); }
	eq(other: AttachmentsWidget) { return other.key === this.key; }
	toDOM() {
		const host = document.createElement('div');
		host.className = 'cm-email-attachments';
		AttachmentsWidget.cleanup.set(host, this.mountChips(host));
		return host;
	}
	destroy(dom: HTMLElement) { AttachmentsWidget.cleanup.get(dom)?.(); AttachmentsWidget.cleanup.delete(dom); }
	ignoreEvent() { return true; }
}

class HiddenNotesWidget extends WidgetType {
	eq() { return true; }
	toDOM() { const hidden = document.createElement('div'); hidden.className = 'cm-email-notes-hidden'; return hidden; }
	ignoreEvent() { return true; }
}

/** The From line as the person sees it: the other fields' look, a step quieter, and no caret (a block widget holds none). */
class FromWidget extends WidgetType {
	constructor(readonly account: string) { super(); }
	eq(other: FromWidget) { return other.account === this.account; }
	toDOM() {
		const row = document.createElement('div');
		row.className = 'cm-email-from';
		row.title = 'The account this email is sent from';
		const label = document.createElement('span');
		label.className = 'cm-email-head-label';
		label.textContent = 'From:';
		row.append(label, ' ', this.account);
		return row;
	}
	ignoreEvent() { return true; }
}

/** Where the To and Cc lines are in the header. */
function recipientLines(state: EditorState) {
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

/** How many characters at the end of an added address always stay on one line with its "!". */
const ADDED_TAIL = 6;
const NOT_ON_EMAIL = 'Not on the email you’re replying to.';
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

/** The "!" after an address that was not on the email being answered: hover or focus on a desktop, a tap on a phone, shows why. */
class AddedMarkWidget extends WidgetType {
	eq() { return true; }
	toDOM() {
		const mark = document.createElement('button');
		mark.type = 'button';
		mark.className = 'cm-email-added-mark';
		mark.setAttribute('aria-label', NOT_ON_EMAIL);
		mark.append('!');
		const tip = document.createElement('span');
		tip.className = 'cm-email-added-tip';
		tip.setAttribute('aria-hidden', 'true');
		tip.textContent = NOT_ON_EMAIL;
		mark.append(tip);
		// Keep the tip inside the editor, which clips what runs past its right edge: shift it left by however far it would.
		const place = () => {
			tip.style.marginLeft = '0px';
			tip.removeAttribute('data-below');
			const scroller = mark.closest('.cm-scroller')?.getBoundingClientRect();
			const edge = Math.min(scroller?.right ?? window.innerWidth, window.innerWidth);
			const box = tip.getBoundingClientRect();
			if (box.right - (edge - 8) > 0) tip.style.marginLeft = `${-(box.right - (edge - 8))}px`;
			// The hint opens above the line, so the next header line (Subject) stays readable; below only where the editor has no room above.
			if (box.top < (scroller?.top ?? 0) + 4) tip.setAttribute('data-below', '');
		};
		const close = () => { mark.removeAttribute('data-open'); document.removeEventListener('pointerdown', outside, true); };
		const outside = (event: Event) => { if (!mark.contains(event.target as Node)) close(); };
		// A phone has no hover, and Safari never focuses a tapped button, so a tap opens and closes it itself.
		mark.addEventListener('click', () => {
			if (mark.hasAttribute('data-open')) { close(); return; }
			mark.setAttribute('data-open', '');
			place();
			document.addEventListener('pointerdown', outside, true);
		});
		mark.addEventListener('mouseenter', place);
		mark.addEventListener('focus', place);
		mark.addEventListener('blur', close);
		mark.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
		return mark;
	}
	ignoreEvent() { return true; }
}

/** The quiet "Cc" text button at the end of the To line: adds an empty Cc line under it, with the caret in it. */
function addCc(view: EditorView) {
	const { to, cc } = recipientLines(view.state);
	if (!cc && to) {
		view.dispatch({ changes: { from: to.to, insert: '\nCc: ' }, selection: { anchor: to.to + 5 }, scrollIntoView: true, userEvent: 'input' });
	}
	view.focus();
}

class CcButtonWidget extends WidgetType {
	eq() { return true; }
	toDOM(view: EditorView) {
		const button = document.createElement('button');
		button.type = 'button';
		button.className = 'cm-email-cc-add';
		button.textContent = 'Cc';
		button.setAttribute('aria-label', 'Add a Cc line');
		// Keep the editor's selection and focus until the click lands, so the caret ends in the new line.
		button.addEventListener('mousedown', (event) => event.preventDefault());
		button.addEventListener('click', () => addCc(view));
		return button;
	}
	ignoreEvent() { return true; }
}

/** The hint inside an empty Cc field, so the field is visible before anything is typed; the caret sits just before it. */
class AddressHintWidget extends WidgetType {
	eq() { return true; }
	toDOM() {
		const hint = document.createElement('span');
		hint.className = 'cm-email-address-hint';
		hint.textContent = 'name@example.com';
		hint.setAttribute('aria-hidden', 'true');
		return hint;
	}
	ignoreEvent() { return true; }
}

export interface EmailHeaderOptions {
	/** The quiet notes, folded into one line under the header. */
	quiet: readonly string[];
	/** Entries in To or Cc that are not full email addresses, as typed: each is amber where it sits, with no "!". */
	bad?: readonly string[];
	/** Lowercase addresses on the email being answered; an address in To or Cc that is not one of them is marked as added. Null: nothing to compare with. */
	answered: readonly string[] | null;
	/** Identifies the attachments drawn; the row is redrawn when it changes. */
	attachmentsKey: string;
	/** Draws the attachment chips; none means no row. */
	mountAttachments: MountAttachments | null;
}

function build(state: EditorState, { quiet, bad = [], answered, attachmentsKey, mountAttachments }: EmailHeaderOptions): DecorationSet {
	const decos: Range<Decoration>[] = [];
	const { doc } = state;
	// The Cc button shows while the header has no Cc line and the draft can be edited; once the line is there it is the field.
	const recipients = recipientLines(state);
	const editable = !state.readOnly && state.facet(EditorView.editable);
	const ccButtonAt = !recipients.cc && editable ? recipients.to : null;
	let lastShown = 0;
	let noteRun: { from: number; to: number } | null = null;
	let end = 1;
	// The header is every line up to the first blank line, as the backend's parse_document reads it, so a line being typed
	// (`C` on its way to `Cc:`) keeps the header look and the fields below it do not jump to body size.
	for (let n = 1; n <= doc.lines; n++) {
		const line = doc.line(n);
		if (line.text === '') break;
		end = n + 1;
		const match = FIELD.exec(line.text);
		const key = match ? match[1].toLowerCase() : '';
		if (key === 'note') {
			noteRun = noteRun ? { from: noteRun.from, to: line.to } : { from: line.from, to: line.to };
			continue;
		}
		if (noteRun) { decos.push(Decoration.replace({ widget: new HiddenNotesWidget(), block: true }).range(noteRun.from, noteRun.to)); noteRun = null; }
		lastShown = n;
		if (key === 'from' && match) {
			decos.push(Decoration.replace({ widget: new FromWidget(line.text.slice(match[0].length).trim()), block: true }).range(line.from, line.to));
			continue;
		}
		const ccButton = ccButtonAt && ccButtonAt.number === n ? ' cm-email-head-to-cc' : '';
		// An empty Cc line is an open field: outlined, with a hint, while the draft can be edited.
		const emptyCc = key === 'cc' && match && !line.text.slice(match[0].length).trim() && editable;
		decos.push(Decoration.line({ class: `cm-email-head ${!key || ADDRESS_FIELDS.has(key) ? 'cm-email-head-address' : ''}${ccButton}${emptyCc ? ' cm-email-head-empty' : ''}` }).range(line.from));
		if (ccButton) decos.push(Decoration.widget({ widget: new CcButtonWidget(), side: 1 }).range(line.to));
		if (emptyCc) decos.push(Decoration.widget({ widget: new AddressHintWidget(), side: 1 }).range(line.to));
		if (match) decos.push(Decoration.mark({ class: 'cm-email-head-label' }).range(line.from, line.from + match[0].length));
		if (match && bad.length && (key === 'to' || key === 'cc')) {
			for (const entry of badRecipientRanges(line.text.slice(match[0].length), line.from + match[0].length, new Set(bad))) decos.push(Decoration.mark({ class: 'cm-email-added' }).range(entry.from, entry.to));
		}
		if (match && answered && (key === 'to' || key === 'cc')) {
			for (const added of addedRecipients(line.text.slice(match[0].length), line.from + match[0].length, new Set(answered))) {
				// The last characters of the address and the "!" are one no-wrap piece, so the "!" is never alone on a line (the rest of the address still breaks anywhere).
				const tailFrom = Math.max(added.from, added.to - ADDED_TAIL);
				if (tailFrom > added.from) decos.push(Decoration.mark({ class: 'cm-email-added' }).range(added.from, tailFrom));
				decos.push(Decoration.mark({ class: 'cm-email-added-tail', inclusiveEnd: true }).range(tailFrom, added.to));
				decos.push(Decoration.widget({ widget: new AddedMarkWidget(), side: -1 }).range(added.to));
			}
		}
	}
	if (noteRun) decos.push(Decoration.replace({ widget: new HiddenNotesWidget(), block: true }).range(noteRun.from, noteRun.to));
	if (!lastShown) return Decoration.none;
	const last = doc.line(lastShown);
	if (quiet.length) {
		// The divider closes the quiet line instead of the last field.
		decos.push(Decoration.line({ class: 'cm-email-head-before-notes' }).range(last.from));
		decos.push(Decoration.widget({ widget: new QuietNotesWidget(quiet), block: true, side: 1 }).range(last.to));
	} else {
		decos.push(Decoration.line({ class: 'cm-email-head-last' }).range(last.from));
	}
	if (mountAttachments) {
		// Directly below the body, above the signature divider (`-- `), or at the end when there is no signature.
		let signature = -1;
		for (let n = doc.lines; n > end; n--) if (/^--\s?$/.test(doc.line(n).text)) { signature = doc.line(n).from; break; }
		const widget = new AttachmentsWidget(attachmentsKey, mountAttachments);
		decos.push(signature >= 0 ? Decoration.widget({ widget, block: true, side: -1 }).range(signature) : Decoration.widget({ widget, block: true, side: 1 }).range(doc.length));
	}
	// The blank line after the header is the gap between the divider and the body.
	if (end <= doc.lines && doc.line(end).text === '') decos.push(Decoration.line({ class: 'cm-email-head-gap' }).range(doc.line(end).from));
	return Decoration.set(decos, true);
}

function field(options: EmailHeaderOptions) {
	return StateField.define<DecorationSet>({
		create: (state) => build(state, options),
		update: (deco, tr) => (tr.docChanged || tr.reconfigured ? build(tr.state, options) : deco),
		provide: (f) => EditorView.decorations.from(f),
	});
}

/** The From line's range, or null. A person's edit that touches it is dropped; an agent's or a remote write is not. */
function fromLine(state: EditorState): { from: number; to: number } | null {
	for (let n = 1; n <= state.doc.lines; n++) {
		const line = state.doc.line(n);
		if (line.text === '') return null;
		if (/^from\s*:/i.test(line.text)) return { from: line.from, to: line.to };
	}
	return null;
}

const keepFrom = EditorState.transactionFilter.of((tr: Transaction) => {
	if (!tr.docChanged || !(tr.isUserEvent('input') || tr.isUserEvent('delete') || tr.isUserEvent('undo') || tr.isUserEvent('redo') || tr.isUserEvent('move'))) return tr;
	const from = fromLine(tr.startState);
	if (!from) return tr;
	let touches = false;
	tr.changes.iterChangedRanges((start, end) => { if (start <= from.to && end >= from.from) touches = true; });
	return touches ? [] : tr;
});

const theme = EditorView.theme({
	// A wrapped value hangs under the value column, not under the label.
	'.cm-line.cm-email-head': { fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)', color: 'var(--text-secondary)', paddingLeft: 'calc(5.2em + 0.27em + 2px)', textIndent: 'calc(-5.2em - 0.27em)' },
	// The Cc button sits at the right end of the To line; the line keeps room for it, so a long address clips before it.
	'.cm-line.cm-email-head-to-cc': { position: 'relative', paddingRight: '2.6em' },
	'.cm-email-cc-add': {
		all: 'unset', boxSizing: 'border-box', cursor: 'pointer', position: 'absolute', right: '2px', top: '0', zIndex: '1',
		padding: '0 var(--space-1)', textIndent: '0', borderRadius: 'var(--radius-sm)', color: 'var(--text-tertiary)',
		fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)',
		'&:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '1px' },
	},
	'.cm-line.cm-email-head-empty': { borderRadius: 'var(--radius-sm)', boxShadow: 'inset 0 0 0 1px var(--border-subtle)' },
	// An address that was not on the email being answered: amber, with a "!" that says so.
	'.cm-email-added, .cm-email-added-tail': { color: 'var(--amber-text)' },
	'.cm-email-added-tail': { whiteSpace: 'nowrap' },
	'.cm-email-added-mark': {
		all: 'unset', boxSizing: 'border-box', cursor: 'help', position: 'relative', display: 'inline-block', textIndent: '0', margin: '0 0 0 var(--space-1)',
		minWidth: '1.15em', textAlign: 'center', borderRadius: '999px', boxShadow: 'inset 0 0 0 1px var(--amber-text)', color: 'var(--amber-text)',
		fontSize: 'var(--text-footnote)', lineHeight: '1.15', fontWeight: 'var(--fw-semibold)',
		'&:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '1px' },
	},
	'.cm-email-added-tip': {
		display: 'none', position: 'absolute', left: '0', bottom: '100%', zIndex: '5', marginBottom: 'var(--space-1)', width: 'max-content', maxWidth: 'min(20em, 80vw)',
		padding: 'var(--space-1) var(--space-2)', borderRadius: 'var(--radius-sm)', background: 'var(--bg-elevated)', color: 'var(--text-primary)',
		boxShadow: 'var(--shadow-chip)', border: '1px solid var(--border-subtle)', fontWeight: 'var(--fw-regular)', textAlign: 'left', whiteSpace: 'normal', cursor: 'default',
	},
	'.cm-email-added-tip[data-below]': { bottom: 'auto', top: '100%', marginBottom: '0', marginTop: 'var(--space-1)' },
	'.cm-email-added-mark[data-open] .cm-email-added-tip, .cm-email-added-mark:focus-visible .cm-email-added-tip': { display: 'block' },
	'.cm-email-address-hint': { textIndent: '0', color: 'var(--text-tertiary)', pointerEvents: 'none', userSelect: 'none' },
	'.cm-email-head-label': { display: 'inline-block', minWidth: '5.2em', textIndent: '0', color: 'var(--text-tertiary)' },
	'.cm-email-from': {
		padding: '0 2px', fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)', color: 'var(--text-tertiary)',
		cursor: 'default', whiteSpace: 'pre', overflow: 'hidden', textOverflow: 'ellipsis',
	},
	'.cm-line.cm-email-head-last': { borderBottom: '1px solid var(--border-subtle)', paddingBottom: 'var(--space-2)' },
	'.cm-line.cm-email-head-before-notes': { paddingBottom: 'var(--space-1)' },
	'.cm-line.cm-email-head-gap': { height: 'var(--space-3)', lineHeight: '0', overflow: 'hidden' },
	'.cm-email-attachments': { padding: '0 2px' },
	'.cm-email-notes': { borderBottom: '1px solid var(--border-subtle)', padding: '0 2px var(--space-2)', fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)', color: 'var(--text-tertiary)' },
	'.cm-email-notes-toggle': {
		all: 'unset', boxSizing: 'border-box', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 'var(--space-1)',
		borderRadius: 'var(--radius-sm)', color: 'var(--text-tertiary)',
		'&:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '2px' },
		'& svg': { transition: 'transform var(--dur-tight)' },
		'&[aria-expanded="true"] svg': { transform: 'rotate(90deg)' },
	},
	'@media (hover: hover)': { '.cm-email-added-mark:hover .cm-email-added-tip': { display: 'block' }, '.cm-email-notes-toggle:hover': { color: 'var(--text-primary)' }, '.cm-email-cc-add:hover': { color: 'var(--text-primary)' } },
	'.cm-email-notes-list': { margin: 'var(--space-1) 0 0', padding: '0', listStyle: 'none', overflowWrap: 'anywhere', '& li': { margin: '0 0 var(--space-1)' } },
	// On a phone the line keeps wrapping (the default is already wrapping on the desktop): a long address breaks anywhere, never cut.
	'@media (max-width: 768px)': { '.cm-line.cm-email-head-address': { whiteSpace: 'pre-wrap', overflow: 'visible', overflowWrap: 'anywhere' } },
	// A finger needs more than a footnote-sized word: the hit area grows past the line without moving the text.
	'@media (pointer: coarse)': { '.cm-email-notes-toggle': { minHeight: 'var(--touch-target)' }, '.cm-email-cc-add::after': { content: '""', position: 'absolute', inset: '-10px -6px' }, '.cm-email-added-mark::after': { content: '""', position: 'absolute', inset: '-12px -8px' } },
	'@media (prefers-reduced-motion: reduce)': { '.cm-email-notes-toggle svg': { transition: 'none' } },
});

/** The compact header for an email draft; reconfigure it when the notes or attachments change. */
export function emailHeader(options: EmailHeaderOptions): Extension {
	return [field(options), keepFrom, theme, showAutolinkMarks.of(true)];
}
