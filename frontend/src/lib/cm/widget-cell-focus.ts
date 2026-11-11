import type { EditorView } from '@codemirror/view';
import { touchMovedBeyond } from './widget-selection-types';

/**
 * What a tap on a table needs beyond opening a cell: whether it was a swipe, and keeping the caret in the cell it opened.
 *
 * A finger that comes down on a table may be starting a scroll, and a click that follows it, whether or not one is sent, is
 * not the reader asking for a caret in a cell.
 *
 * Keeps the caret in a cell that a click just opened.
 *
 * The click may also open a comment card whose sheet focuses itself a tick later, which would leave the caret nowhere typed
 * text can land. Focus a panel took on its own in that moment goes back to the cell, up to three times; focus that moves to another editor
 * or a field the reader chose is never taken back (the document's own text, which the tap focuses too, is not one), which would be a tug of war. A press elsewhere closes the cell first.
 */
export class CellFocusKeeper {
	private until = 0;
	private left = 0;
	private start: { x: number; y: number } | null = null;
	private moved = false;

	constructor(private readonly host: EditorView, private readonly editor: () => EditorView | null) {
		document.addEventListener('focusin', this.focusTaken, true);
		host.dom.addEventListener('pointerdown', this.down, true);
		host.dom.addEventListener('pointermove', this.move, true);
	}

	/** True when the press the click being handled ends moved past the touch slop. */
	swiped() { return this.moved; }

	/** A cell was just opened by a click: for a moment, a panel opening from the same click may not keep the focus. */
	opened() { this.until = performance.now() + 600; this.left = 3; }

	destroy() {
		document.removeEventListener('focusin', this.focusTaken, true);
		this.host.dom.removeEventListener('pointerdown', this.down, true);
		this.host.dom.removeEventListener('pointermove', this.move, true);
	}

	private readonly down = (event: PointerEvent) => {
		this.moved = false;
		this.start = event.pointerType === 'touch' ? { x: event.clientX, y: event.clientY } : null;
	};

	private readonly move = (event: PointerEvent) => {
		if (this.start && touchMovedBeyond(this.start, { x: event.clientX, y: event.clientY })) this.moved = true;
	};

	/** Focus on a field or another editor is the reader's own; the document's own text, which a tap also focuses, is not. */
	private chosen(target: Element) {
		const own = this.host.dom;
		return own.contains(target) ? false : !!target.closest('.cm-editor, input, textarea, [contenteditable]');
	}

	private readonly focusTaken = (event: FocusEvent) => {
		const editor = this.editor();
		if (!editor || performance.now() > this.until) return;
		const target = event.target;
		if (!(target instanceof Element) || editor.dom.contains(target) || this.chosen(target)) return;
		// A few times at most, so two editors can never pass the focus back and forth.
		if (--this.left <= 0) this.until = 0;
		editor.focus();
	};
}
