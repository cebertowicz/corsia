import { EditorView, WidgetType } from '@codemirror/view';
import { cubicOut } from 'svelte/easing';
import { slide } from 'svelte/transition';
import { recipientLines } from './email-header-recipients';

// The pieces the compact email header draws over the draft text: quiet notes, the attachments row, the From line,
// the "!" on an added address, the Cc button and the empty-Cc hint. `email-header.ts` decides where each one goes.

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
export class QuietNotesWidget extends WidgetType {
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
export class AttachmentsWidget extends WidgetType {
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

export class HiddenNotesWidget extends WidgetType {
	eq() { return true; }
	toDOM() { const hidden = document.createElement('div'); hidden.className = 'cm-email-notes-hidden'; return hidden; }
	ignoreEvent() { return true; }
}

/** The From line as the person sees it: the other fields' look, a step quieter, and no caret (a block widget holds none). */
export class FromWidget extends WidgetType {
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

const NOT_ON_EMAIL = 'Not on the email you’re replying to.';

/** Where the hint can go on the mark's own row: the left edge of whatever is to its right (the next address, its comma, the Cc button; null when the row ends at the mark) and of this entry's own address text before it. */
function rowNeighbours(mark: HTMLElement, row: DOMRect): { next: number | null; start: number } {
	const line = mark.closest('.cm-line');
	let next: number | null = null;
	let start = row.left;
	if (!line) return { next, start };
	const onRow = (box: DOMRect) => box.bottom > row.top && box.top < row.bottom;
	const texts: Text[] = [];
	const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
	for (let node = walker.nextNode(); node; node = walker.nextNode()) if (!mark.contains(node) && !node.parentElement?.closest('.cm-email-cc-add')) texts.push(node as Text);
	const rects = (node: Text) => {
		const range = document.createRange();
		range.selectNodeContents(node);
		return [...range.getClientRects()].filter(onRow);
	};
	const after = texts.filter((node) => /\S/.test(node.data) && mark.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
	for (const node of after) for (const box of rects(node)) next = Math.min(next ?? box.left, box.left);
	const button = line.querySelector('.cm-email-cc-add')?.getBoundingClientRect();
	if (button && onRow(button)) next = Math.min(next ?? button.left, button.left);
	// The amber pieces just before the mark are this entry; the first other text ends it.
	const before = texts.filter((node) => mark.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_PRECEDING).reverse();
	for (const node of before) {
		if (!node.parentElement?.closest('.cm-email-added, .cm-email-added-tail')) break;
		for (const box of rects(node)) start = Math.min(start, box.left);
	}
	return { next, start };
}

/** The "!" after an address that was not on the email being answered: hover or focus on a desktop, a tap on a phone, shows why. */
export class AddedMarkWidget extends WidgetType {
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
		// The hint is one line on the mark's own row, centered on it: just right of the "!" when nothing else is on the row to its right and it fits,
		// else ending at the "!" and opening leftward over this entry's own address text, else (neither is wide enough) as close to the "!" as fits,
		// clamped inside the editor's edges, where it may cover other text on that row. It never wraps and never grows past the row.
		const place = () => {
			tip.style.left = '';
			tip.setAttribute('data-side', 'right');
			const scroller = mark.closest('.cm-scroller')?.getBoundingClientRect();
			const inner = { left: (scroller?.left ?? 0) + 8, right: Math.min(scroller?.right ?? window.innerWidth, window.innerWidth) - 8 };
			const row = mark.getBoundingClientRect();
			const { next, start } = rowNeighbours(mark, row);
			const box = tip.getBoundingClientRect();
			if (box.right <= Math.min(inner.right, next ?? inner.right)) return;
			if (box.width <= row.left - start) { tip.setAttribute('data-side', 'left'); return; }
			tip.setAttribute('data-side', 'clamp');
			tip.style.left = `${Math.max(inner.left, Math.min(box.left, inner.right - box.width)) - row.left}px`;
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

export class CcButtonWidget extends WidgetType {
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
export class AddressHintWidget extends WidgetType {
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
