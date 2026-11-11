import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { caretAt, selectInWidget, touchMovedBeyond, type WidgetSelection } from './widget-selection-types';
export { touchMovedBeyond, type WidgetSelection } from './widget-selection-types';
import { commentRanges } from './comment-marks';
import { pendingCommentMark } from './pending-comment-mark';
import { drawnCellImage, markChangedOpenable, openWidgetImage, openWidgetOnKey } from './widget-open';
import { blockRange, DIAGRAM, TABLE, widgetOf } from './widget-blocks';
import { selectionIn } from './widget-selection';
import { CELL_EDITOR } from './widget-cell-state';
import { cellClicked } from './widget-cell-click';
import { addCommentId, commentIdsAt, keepingSelection, paintTable, unpaint, type Mark } from './widget-paint';
import { caretToWidget } from './widget-caret';

/**
 * Comments on text inside a rendered table, and on a rendered diagram as a whole. The widgets replace their source,
 * so the browser's own selection inside them never reaches the editor: this reads it, maps it into the block's own
 * source range, and raises the same selection the editor raises for text. It also paints the comment marks inside the
 * widget, because the widget replaces the range the mark decorations would sit on.
 */
export interface WidgetCommentHooks {
	onSelect: (selection: WidgetSelection | null) => void;
	/** Every comment drawn where the click landed, with the painted element it landed on; several open the list, as the margin marker does. */
	onFocus: (ids: string[], opener: HTMLElement, click: MouseEvent) => void;
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
			// A touch may be the start of a scroll, so its caret waits for the click that confirms a tap.
			if (event.button === 0 && event.pointerType !== 'touch') caretToWidget(this.view, event);
			if (event.pointerType === 'touch') { this.touchStart = { x: event.clientX, y: event.clientY }; this.touchMoved = false; this.lastTouchAt = Date.now(); }
		};
		private readonly move = (event: PointerEvent) => {
			if (event.pointerType !== 'touch' || !this.touchStart) return;
			if (touchMovedBeyond(this.touchStart, { x: event.clientX, y: event.clientY })) this.touchMoved = true;
		};
		private frame = 0;
		private readonly listen = () => { if (!this.frame) this.frame = requestAnimationFrame(() => { this.frame = 0; this.read(); }); };
		private readonly click = (event: MouseEvent) => {
			if (this.pointer === 'touch' && !this.touchMoved) caretToWidget(this.view, event);
			const origin = event.target instanceof Element ? event.target : null;
			// A click inside an open cell editor belongs to that editor: it places a caret there, and its own selection is read
			// by `read` below like any other selection made inside the widget.
			if (origin?.closest(CELL_EDITOR)) return;
			const widget = origin && widgetOf(this.view, origin);
			const selection = document.getSelection();
			const ids = widget && origin ? commentIdsAt(widget, origin) : [];
			// A whole-diagram text comment takes the click; the viewer remains reachable by keyboard and from its pins.
			if (widget?.matches(DIAGRAM) && origin && ids.length > 0 && selection?.isCollapsed !== false) { hooks.onFocus(ids, origin.closest<HTMLElement>('[data-comment-ids]') ?? widget, event); return; }
			// A cell image or a diagram opens the viewer on a click, as an image in the text does; a drag across a diagram is a selection, not a click.
			if (widget && origin && !origin.closest('a') && (drawnCellImage(origin) || selection?.isCollapsed !== false) && openWidgetImage(this.view, origin)) { event.preventDefault(); return; }
			if (widget && origin && ids.length > 0 && selection?.isCollapsed !== false) { hooks.onFocus(ids, origin.closest<HTMLElement>('[data-comment-ids]') ?? widget, event); return; }
			if ((this.pointer === 'touch' || this.recentTouch()) && (!origin || !widgetOf(this.view, origin))) { hooks.onSelect(null); return; }
			this.tap(event);
		};

		// A touch has no drag to select with, and a long press inside a widget yields no selection the editor can read. A tap on a cell
		// selects that cell and a tap on a diagram selects the diagram, so Comment is reachable without a selection to draw.
		private tap(event: MouseEvent) {
			if ((this.pointer !== 'touch' && !this.recentTouch()) || this.touchMoved) return;
			const target = event.target instanceof Element ? event.target : null;
			const widget = target && widgetOf(this.view, target);
			// A tap that edits a cell is the cell editor's: selecting the cell's text here would take the caret off its editor.
			if (!widget || target.closest('a') || target.closest(CELL_EDITOR) || cellClicked(this.view, target, { x: event.clientX, y: event.clientY })) return;
			const cell = widget.matches(TABLE) ? target.closest('td, th') : null;
			if (widget.matches(TABLE) && !cell) return;
			const range = document.createRange();
			if (cell) range.selectNodeContents(cell); else range.selectNodeContents(widget);
			selectInWidget(this.view, (selection) => { selection.removeAllRanges(); selection.addRange(range); });
			this.listen();
		}

		// The first press in a widget is turned into a caret by the editor's focus, which drops the drag; later presses select. A press that
		// starts in a widget therefore draws its own selection (press, drag, release) and leaves links and double clicks to the browser.
		private readonly press = (event: MouseEvent) => {
			const target = event.target as Element;
			if (event.button !== 0 || event.shiftKey || event.detail > 1 || !widgetOf(this.view, target) || target.closest('a') || target.closest(CELL_EDITOR)) return;
			const widget = widgetOf(this.view, target)!;
			// A tap is read on its click; its mouse press must not focus the editor, which would collapse the selection the tap makes.
			if (this.pointer === 'touch' || this.recentTouch()) { event.preventDefault(); return; }
			if (widget.matches(DIAGRAM)) {
				// A diagram's text is not the unit: any drag across it selects the diagram, which is what gets the comment.
				const { clientX, clientY } = event;
				const release = (e: MouseEvent) => {
					window.removeEventListener('mouseup', release);
					if (Math.hypot(e.clientX - clientX, e.clientY - clientY) > 4) { selectInWidget(this.view, (selection) => selection.selectAllChildren(widget)); this.listen(); }
				};
				window.addEventListener('mouseup', release);
				return;
			}
			const emptyCell = target.closest<HTMLTableCellElement>('td, th');
			// An image's cell has no text but is not empty: a press on the image is the first half of a click that opens it.
			if (drawnCellImage(target)) return;
			// Chromium gives an empty cell a collapsed DOM range but does not announce it through selectionchange.
			// This is the existing press path for table selection, so read the range synchronously here.
			if (emptyCell && !emptyCell.textContent?.trim()) {
				event.preventDefault();
				const range = document.createRange();
				range.selectNodeContents(emptyCell);
				selectInWidget(this.view, (selection) => { selection.removeAllRanges(); selection.addRange(range); });
				const release = () => { window.removeEventListener('mouseup', release); this.listen(); };
				window.addEventListener('mouseup', release);
				return;
			}
			const start = caretAt(event.clientX, event.clientY);
			if (!start) return;
			event.preventDefault();
			selectInWidget(this.view, (selection) => selection.setBaseAndExtent(start.node, start.offset, start.node, start.offset));
			// Painting the pending mark rewraps the cell's text and detaches start.node, which freezes the drag, so paint once on release.
			this.dragging = true;
			const move = (e: MouseEvent) => {
				// A release outside the window sends no mouseup, so the drag ends when a move arrives with no button held.
				if (e.buttons === 0) {
					up();
					return;
				}
				const at = caretAt(e.clientX, e.clientY);
				if (at && widget.contains(at.node)) selectInWidget(this.view, (selection) => selection.setBaseAndExtent(start.node, start.offset, at.node, at.offset));
			};
			const up = () => { this.dragging = false; window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); this.listen(); };
			window.addEventListener('mousemove', move);
			window.addEventListener('mouseup', up);
		};

		private readonly key = (event: KeyboardEvent) => {
			// Keys inside an open cell editor are the editor's: Tab, Enter and Escape move between cells there.
			if (event.target instanceof Element && event.target.closest(CELL_EDITOR)) return;
			if (openWidgetOnKey(this.view, event)) { event.preventDefault(); event.stopPropagation(); }
		};
		private readonly rendered = () => this.paint();
		private readonly observer = new MutationObserver(this.rendered);
		private readonly openableSignatures = new WeakMap<HTMLElement, string>();

		constructor(private readonly view: EditorView) {
			view.dom.addEventListener('pointerdown', this.down, true);
			view.dom.addEventListener('pointermove', this.move, true);
			view.dom.addEventListener('mousedown', this.press, true);
			document.addEventListener('selectionchange', this.listen);
			view.dom.addEventListener('mouseup', this.listen);
			view.dom.addEventListener('click', this.click);
			view.dom.addEventListener('keydown', this.key, true);
			view.dom.addEventListener('load', this.rendered, true);
			view.dom.addEventListener('error', this.rendered, true);
			this.observer.observe(view.contentDOM, { childList: true, subtree: true });
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
			this.view.dom.removeEventListener('keydown', this.key, true);
			this.view.dom.removeEventListener('load', this.rendered, true);
			this.view.dom.removeEventListener('error', this.rendered, true);
			this.observer.disconnect();
		}

		private dragging = false;

		private read() {
			if (this.dragging) return;
			const selection = document.getSelection();
			const range = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
			const el = range && widgetOf(this.view, range.commonAncestorContainer);
			const raised = el && range && (!range.collapsed || el.matches(TABLE)) ? selectionIn(this.view, el, range) : null;
			// While the editor holds focus, every editor update (this selection's own pending mark, a collaborator's edit)
			// writes the editor's caret over the browser's selection, which then reads as nothing. A selection inside a widget
			// is not the editor's, so the editor gives up focus and the same range is put back.
			if (raised && this.view.hasFocus) {
				const kept = range!.cloneRange();
				selectInWidget(this.view, (selected) => { selected.removeAllRanges(); selected.addRange(kept); });
			}
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
					// An open suggestion with a quote is drawn in its cell as it is in body text; a pure insertion has no quote to strike and a card-only one is not drawn.
					const marks: Mark[] = view.state.field(commentRanges).filter((range) => !range.point && (!range.suggestion || (range.suggestion.inline && range.from < range.to))).map((range) => ({ from: range.from, to: range.to, id: range.id, kind: range.focused ? 'focused' : 'placed', region: range.region, suggestion: range.suggestion && { insert: range.suggestion.insert, color: range.suggestion.color, busy: range.suggestion.busy } }));
					if (pending) marks.push({ ...pending, id: 'pending', kind: 'pending' });
					return { widgets, marks };
				},
				write: ({ widgets, marks }) => {
					for (const { el, block, src } of widgets) {
						const mine = marks.filter((mark) => mark.from < block.to && mark.to > block.from);
						const signature = JSON.stringify([block.from, src.length, mine]);
						if (el.dataset.paintSignature !== signature) {
							el.dataset.paintSignature = signature;
							keepingSelection(el, () => {
								unpaint(el);
								if (el.matches(DIAGRAM)) {
									for (const mark of mine.filter((item) => !item.region)) {
										el.classList.add(mark.kind === 'pending' ? 'cm-comment-block-pending' : 'cm-comment-block', ...(mark.kind === 'focused' ? ['cm-comment-block-focused'] : []));
										addCommentId(el, mark);
										el.dataset.commentAria = 'true';
										el.setAttribute('role', 'img');
										el.setAttribute('aria-label', 'Commented diagram');
									}
								} else paintTable(el, block.from, src, mine);
							});
						}
						markChangedOpenable(el, signature, this.view, this.openableSignatures);
					}
				},
			});
		}
	});
}
