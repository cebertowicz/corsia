import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { commentRanges } from './comment-marks';
import { pendingCommentMark } from './pending-comment-mark';
import { blockRange, cellElement, DIAGRAM, TABLE, widgetOf } from './widget-blocks';
import { occurrences, snappedTableRows, tableCells, tableCommentLabel, type CellSpan } from './widget-cells';
import { paintTable, unpaint, type Mark } from './widget-paint';

/**
 * Comments on text inside a rendered table, and on a rendered diagram as a whole. The widgets replace their source,
 * so the browser's own selection inside them never reaches the editor: this reads it, maps it into the block's own
 * source range, and raises the same selection the editor raises for text. It also paints the comment marks inside the
 * widget, because the widget replaces the range the mark decorations would sit on.
 */

const TOUCH_SLOP = 6;

/** A scroll or horizontal pan is not the tap fallback for a widget comment. */
export const touchMovedBeyond = (start: { x: number; y: number }, current: { x: number; y: number }) => Math.hypot(current.x - start.x, current.y - start.y) > TOUCH_SLOP;

export interface WidgetSelection {
	from: number; to: number; text: string; top: number; left: number;
	anchor: () => DOMRect | null;
	label?: string;
	/** The Comment button sits on the selection's top edge instead of beside its end, so it never covers text in the selected row. */
	above?: boolean;
}

function caretAt(x: number, y: number): { node: Node; offset: number } | null {
	const position = document.caretPositionFromPoint?.(x, y);
	if (position) return { node: position.offsetNode, offset: position.offset };
	const range = document.caretRangeFromPoint?.(x, y);
	return range ? { node: range.startContainer, offset: range.startOffset } : null;
}

/** Maps what the browser has selected inside a widget to source offsets, or null when there is nothing to comment on. */
function selectionIn(view: EditorView, el: HTMLElement, range: Range): WidgetSelection | null {
	const block = blockRange(view, el);
	if (!block || block.to <= block.from) return null;
	const rects = [...range.getClientRects()];
	const end = rects[rects.length - 1] ?? range.getBoundingClientRect();
	const top = Math.min(...(rects.length ? rects : [end]).map((r) => r.top));
	const place = { top, left: end.right, above: true };
	if (el.matches(DIAGRAM)) {
		return { ...block, text: view.state.sliceDoc(block.from, block.to), ...place, label: 'The whole diagram', anchor: () => el.isConnected ? el.getBoundingClientRect() : null };
	}
	const src = view.state.sliceDoc(block.from, block.to);
	const covered = tableCells(src).map((span) => ({ span, cell: cellElement(el, span) }))
		.filter((hit): hit is { span: CellSpan; cell: HTMLTableCellElement } => !!hit.cell && range.intersectsNode(hit.cell))
		.map(({ span, cell }) => {
			const part = range.cloneRange();
			if (part.comparePoint(cell, 0) < 0) part.setStart(cell, 0);
			if (part.comparePoint(cell, cell.childNodes.length) > 0) part.setEnd(cell, cell.childNodes.length);
			return { span, cell, part, picked: part.toString() };
		})
		.filter(({ picked }) => picked.trim() !== '');
	if (covered.length === 0) return null;
	const rows = (items: Array<{ cell: HTMLTableCellElement }> = covered) => {
		const trs = items.map(({ cell }) => cell.parentElement).filter((tr): tr is HTMLElement => !!tr?.isConnected);
		if (trs.length === 0) return null;
		const top = Math.min(...trs.map((tr) => tr.getBoundingClientRect().top));
		const bottom = Math.max(...trs.map((tr) => tr.getBoundingClientRect().bottom));
		const wide = el.getBoundingClientRect();
		return new DOMRect(wide.left, top, wide.width, bottom - top);
	};
	const first = covered[0], last = covered[covered.length - 1];
	// The label comes from the one shared function, from the same offsets and quote the card is later given.
	const whole = (start: { span: CellSpan; cell: HTMLTableCellElement } = first, end: { span: CellSpan; cell: HTMLTableCellElement } = last, items: Array<{ cell: HTMLTableCellElement }> = covered): WidgetSelection => {
		const from = block.from + start.span.from, to = block.from + end.span.to, text = src.slice(start.span.from, end.span.to);
		return { from, to, text, ...place, label: tableCommentLabel(view.state, from, to, text) ?? undefined, anchor: () => rows(items) };
	};
	const snapped = snappedTableRows(src, covered.map(({ span }) => span));
	if (snapped && snapped.rows > 1) {
		const all = snapped.cells.map((span) => ({ span, cell: cellElement(el, span) }))
			.filter((hit): hit is { span: CellSpan; cell: HTMLTableCellElement } => !!hit.cell);
		return whole(all[0] ?? first, all[all.length - 1] ?? last, all);
	}
	if (covered.length > 1) return whole();
	// One cell: the occurrence the person selected, by its place among the same text in the rendered cell.
	const { span, cell, part, picked } = first;
	const rendered = cell.textContent ?? '';
	const before = document.createRange();
	before.selectNodeContents(cell);
	before.setEnd(part.startContainer, part.startOffset);
	const index = occurrences(rendered, picked).filter((at) => at < before.toString().length).length;
	const inSource = occurrences(src.slice(span.from, span.to), picked);
	if (inSource.length === occurrences(rendered, picked).length && inSource[index] !== undefined) {
		const from = block.from + span.from + inSource[index];
		return { from, to: from + picked.length, text: picked, ...place, label: tableCommentLabel(view.state, from, from + picked.length, picked) ?? undefined, anchor: rows as () => DOMRect | null };
	}
	return whole();
}

export interface WidgetCommentHooks {
	onSelect: (selection: WidgetSelection | null) => void;
	onFocus: (id: string) => void;
}

/**
 * Raises the selection made inside a table or diagram widget and paints the comment marks drawn inside them. Register
 * it after `pendingCommentMark` and `commentMarks`, which it reads.
 */
export function widgetComments(hooks: WidgetCommentHooks) {
	return ViewPlugin.fromClass(class {
		private raised = false;
		private pointer = '';
		private touchStart: { x: number; y: number } | null = null;
		private touchMoved = false;
		private lastTouchAt = 0;
		private recentTouch = () => Date.now() - this.lastTouchAt < 750;
		private readonly down = (event: PointerEvent) => {
			this.pointer = event.pointerType;
			if (event.pointerType === 'touch') { this.touchStart = { x: event.clientX, y: event.clientY }; this.touchMoved = false; this.lastTouchAt = Date.now(); }
		};
		private readonly move = (event: PointerEvent) => {
			if (event.pointerType !== 'touch' || !this.touchStart) return;
			if (touchMovedBeyond(this.touchStart, { x: event.clientX, y: event.clientY })) this.touchMoved = true;
		};
		private frame = 0;
		private readonly listen = () => { if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = 0; this.read(); }); };
		private readonly click = (event: MouseEvent) => {
			const origin = event.target instanceof Element ? event.target : null;
			const target = origin?.closest<HTMLElement>('[data-comment-id]') ?? null;
			if (target && widgetOf(this.view, target) && document.getSelection()?.isCollapsed !== false) { hooks.onFocus(target.dataset.commentId!); return; }
			if ((this.pointer === 'touch' || this.recentTouch()) && (!origin || !widgetOf(this.view, origin))) { hooks.onSelect(null); return; }
			this.tap(event);
		};

		// A touch has no drag to select with, and a long press inside a widget yields no selection the editor can read. A tap on a cell
		// selects that cell and a tap on a diagram selects the diagram, so Comment is reachable without a selection to draw.
		private tap(event: MouseEvent) {
			if ((this.pointer !== 'touch' && !this.recentTouch()) || this.touchMoved) return;
			const target = event.target instanceof Element ? event.target : null;
			const widget = target && widgetOf(this.view, target);
			if (!widget || target.closest('a')) return;
			const selection = document.getSelection();
			if (!selection) return;
			const cell = widget.matches(TABLE) ? target.closest('td, th') : null;
			if (widget.matches(TABLE) && !cell) return;
			selection.removeAllRanges();
			const range = document.createRange();
			if (cell) range.selectNodeContents(cell); else range.selectNodeContents(widget);
			selection.addRange(range);
			this.listen();
		}

		// The first press in a widget is turned into a caret by the editor's focus, which drops the drag; later presses select. A press that
		// starts in a widget therefore draws its own selection (press, drag, release) and leaves links and double clicks to the browser.
		private readonly press = (event: MouseEvent) => {
			const target = event.target as Element;
			if (event.button !== 0 || event.shiftKey || event.detail > 1 || !widgetOf(this.view, target) || target.closest('a')) return;
			const widget = widgetOf(this.view, target)!;
			// A tap is read on its click; its mouse press must not focus the editor, which would collapse the selection the tap makes.
			if (this.pointer === 'touch' || this.recentTouch()) { event.preventDefault(); return; }
			if (widget.matches(DIAGRAM)) {
				// A diagram's text is not the unit: any drag across it selects the diagram, which is what gets the comment.
				const { clientX, clientY } = event;
				const release = (e: MouseEvent) => {
					window.removeEventListener('mouseup', release);
					if (Math.hypot(e.clientX - clientX, e.clientY - clientY) > 4) { document.getSelection()?.selectAllChildren(widget); this.listen(); }
				};
				window.addEventListener('mouseup', release);
				return;
			}
			const start = caretAt(event.clientX, event.clientY);
			if (!start) return;
			event.preventDefault();
			const selection = document.getSelection()!;
			selection.setBaseAndExtent(start.node, start.offset, start.node, start.offset);
			const move = (e: MouseEvent) => {
				const at = caretAt(e.clientX, e.clientY);
				if (at && widget.contains(at.node)) selection.setBaseAndExtent(start.node, start.offset, at.node, at.offset);
			};
			const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); this.listen(); };
			window.addEventListener('mousemove', move);
			window.addEventListener('mouseup', up);
		};

		constructor(private readonly view: EditorView) {
			view.dom.addEventListener('pointerdown', this.down, true);
			view.dom.addEventListener('pointermove', this.move, true);
			view.dom.addEventListener('mousedown', this.press, true);
			document.addEventListener('selectionchange', this.listen);
			view.dom.addEventListener('mouseup', this.listen);
			view.dom.addEventListener('click', this.click);
			this.paint();
		}

		update(update: ViewUpdate) {
			if (update.docChanged || update.viewportChanged || update.geometryChanged || update.heightChanged
				|| update.startState.field(commentRanges) !== update.state.field(commentRanges)
				|| update.transactions.some((transaction) => transaction.effects.length > 0)) this.paint();
		}

		destroy() {
			cancelAnimationFrame(this.frame);
			document.removeEventListener('selectionchange', this.listen);
			this.view.dom.removeEventListener('mouseup', this.listen);
			this.view.dom.removeEventListener('pointerdown', this.down, true);
			this.view.dom.removeEventListener('pointermove', this.move, true);
			this.view.dom.removeEventListener('mousedown', this.press, true);
			this.view.dom.removeEventListener('click', this.click);
		}

		private read() {
			const selection = document.getSelection();
			const range = selection && selection.rangeCount > 0 && !selection.isCollapsed ? selection.getRangeAt(0) : null;
			const el = range && widgetOf(this.view, range.commonAncestorContainer);
			const raised = el && range ? selectionIn(this.view, el, range) : null;
			if (raised) { this.raised = true; hooks.onSelect({ ...raised, top: raised.anchor()?.top ?? raised.top }); }
			else if (this.raised) { this.raised = false; hooks.onSelect(null); }
		}

		private paint() {
			this.view.requestMeasure({
				key: this,
				read: (view) => {
					const widgets = [...view.contentDOM.querySelectorAll<HTMLElement>(`${TABLE}, ${DIAGRAM}`)].flatMap((el) => {
						const block = blockRange(view, el);
						return block ? [{ el, block, src: el.matches(TABLE) ? view.state.sliceDoc(block.from, block.to) : '' }] : [];
					});
					const pending = view.plugin(pendingCommentMark)?.range ?? null;
					const marks: Mark[] = view.state.field(commentRanges).filter((range) => !range.suggestion)
						.map((range) => ({ from: range.from, to: range.to, id: range.id, kind: range.focused ? 'focused' : 'placed' }));
					if (pending) marks.push({ ...pending, id: 'pending', kind: 'pending' });
					return { widgets, marks };
				},
				write: ({ widgets, marks }) => {
					for (const { el, block, src } of widgets) {
						const mine = marks.filter((mark) => mark.from < block.to && mark.to > block.from);
						const signature = JSON.stringify([block.from, src.length, mine]);
						if (el.dataset.paintSignature === signature) continue;
						el.dataset.paintSignature = signature;
						unpaint(el);
						if (el.matches(DIAGRAM)) {
							for (const mark of mine) {
								el.classList.add(mark.kind === 'pending' ? 'cm-comment-block-pending' : 'cm-comment-block', ...(mark.kind === 'focused' ? ['cm-comment-block-focused'] : []));
								el.dataset.commentId = mark.id;
								el.dataset.commentAria = 'true';
								el.setAttribute('role', 'img');
								el.setAttribute('aria-label', 'Commented diagram');
							}
						} else paintTable(el, block.from, src, mine);
					}
				},
			});
		}
	});
}
