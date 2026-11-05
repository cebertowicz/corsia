import { cellElement } from './widget-blocks';
import { clean, intersectsCell, occurrences, tableCells } from './widget-cells';

export interface Mark { from: number; to: number; id: string; kind: 'placed' | 'focused' | 'pending' }

const CLASSES = ['cm-comment-anchor', 'cm-comment-focused', 'cm-comment-pending', 'cm-comment-block', 'cm-comment-block-focused', 'cm-comment-block-pending'];

const PAINTED = [...CLASSES.map((name) => `.${name}`), '[data-comment-ids]'].join(',');

/** Every comment drawn on an element, in the order they were painted. A pending document mark has no comment to open, so it adds none; chat opts its queued comments in through `wrapRange`. */
export function addCommentId(el: HTMLElement, mark: Mark) {
	if (mark.kind === 'pending') return;
	const ids = (el.dataset.commentIds ?? '').split(',').filter(Boolean);
	if (!ids.includes(mark.id)) el.dataset.commentIds = [...ids, mark.id].join(',');
}

/** The comments drawn at a click inside a widget: the element it landed on and every painted element around it, up to the widget. */
export function commentIdsAt(widget: HTMLElement, target: Element): string[] {
	const ids: string[] = [];
	for (let el: Element | null = target; el; el = el === widget ? null : el.parentElement) {
		for (const id of (el as HTMLElement).dataset?.commentIds?.split(',') ?? []) if (id && !ids.includes(id)) ids.push(id);
	}
	return ids;
}

export function unpaint(el: HTMLElement) {
	for (const wrap of el.querySelectorAll<HTMLElement>('span.cm-comment-wrap')) wrap.replaceWith(...wrap.childNodes);
	for (const marked of [el, ...el.querySelectorAll<HTMLElement>(PAINTED)]) {
		marked.classList.remove(...CLASSES);
		if (marked.classList.length === 0) marked.removeAttribute('class');
		marked.removeAttribute('data-comment-ids');
		if (marked.dataset.commentAria) { marked.removeAttribute('aria-label'); marked.removeAttribute('role'); marked.removeAttribute('data-comment-aria'); }
	}
	el.normalize();
}

/** A point as the count of text characters before it in `el`. */
function textOffset(el: HTMLElement, node: Node, offset: number): number {
	const before = document.createRange();
	before.setStart(el, 0);
	before.setEnd(node, offset);
	return before.toString().length;
}

/** The point `at` characters into `el`; between two text nodes, a start belongs to the later one and an end to the earlier. */
function textPoint(el: HTMLElement, at: number, start: boolean): { node: Node; offset: number } {
	const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
	let seen = 0, last: Text | null = null;
	for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
		if (start ? at < seen + node.data.length : at <= seen + node.data.length) return { node, offset: at - seen };
		seen += node.data.length;
		last = node;
	}
	return last ? { node: last, offset: last.data.length } : { node: el, offset: el.childNodes.length };
}

/**
 * Runs `repaint` on a widget without losing the browser's selection inside it. Wrapping and unwrapping text removes the
 * nodes a selection sits on, which collapses it; painting never changes the text, so the same characters are selected again.
 */
export function keepingSelection(el: HTMLElement, repaint: () => void) {
	const selection = document.getSelection();
	const { anchorNode, anchorOffset, focusNode, focusOffset } = selection ?? {};
	const inside = !!selection && !selection.isCollapsed && !!anchorNode && !!focusNode && el.contains(anchorNode) && el.contains(focusNode);
	const kept = inside ? { text: selection.toString(), anchor: textOffset(el, anchorNode!, anchorOffset!), focus: textOffset(el, focusNode!, focusOffset!) } : null;
	repaint();
	if (!kept || selection!.toString() === kept.text) return;
	const anchor = textPoint(el, kept.anchor, kept.anchor <= kept.focus), focus = textPoint(el, kept.focus, kept.focus < kept.anchor);
	selection!.setBaseAndExtent(anchor.node, anchor.offset, focus.node, focus.offset);
}

const markClasses = (kind: Mark['kind']) => kind === 'pending' ? ['cm-comment-pending'] : kind === 'focused' ? ['cm-comment-anchor', 'cm-comment-focused'] : ['cm-comment-anchor'];

/**
 * Wraps the text nodes a range holds, so a mark follows the text inside formatting.
 * `interactivePending`: a chat comment queued for sending can still be edited, so its pending mark opens it like a saved one.
 */
export function wrapRange(range: Range, mark: Mark, interactivePending = false) {
	const walker = document.createTreeWalker(range.commonAncestorContainer, NodeFilter.SHOW_TEXT);
	const nodes: Text[] = [];
	for (let node = walker.nextNode(); node; node = walker.nextNode()) if (range.intersectsNode(node)) nodes.push(node as Text);
	if (range.commonAncestorContainer.nodeType === Node.TEXT_NODE) nodes.push(range.commonAncestorContainer as Text);
	for (const text of new Set(nodes)) {
		const part = document.createRange();
		part.selectNodeContents(text);
		if (text === range.startContainer) part.setStart(text, range.startOffset);
		if (text === range.endContainer) part.setEnd(text, range.endOffset);
		if (part.collapsed) continue;
		const wrap = document.createElement('span');
		wrap.className = ['cm-comment-wrap', ...markClasses(mark.kind)].join(' ');
		addCommentId(wrap, mark);
		if (interactivePending && mark.kind === 'pending') wrap.dataset.commentIds = mark.id;
		wrap.dataset.commentAria = 'true';
		wrap.setAttribute('aria-label', `Commented text: ${clean(part.toString())}`);
		part.surroundContents(wrap);
	}
}

export function paintTable(el: HTMLElement, base: number, src: string, marks: Mark[]) {
	const cells = tableCells(src);
	for (const mark of marks) {
		const from = mark.from - base, to = mark.to - base;
		if (from <= 0 && to >= src.length) {
			el.classList.add(mark.kind === 'pending' ? 'cm-comment-block-pending' : 'cm-comment-block', ...(mark.kind === 'focused' ? ['cm-comment-block-focused'] : []));
			addCommentId(el, mark);
			continue;
		}
		const hit = cells.filter((span) => intersectsCell(span, from, to));
		for (const span of hit) {
			const cell = cellElement(el, span);
			if (!cell) continue;
			const inside = hit.length === 1 && (from > span.from || to < span.to);
			const text = src.slice(span.from, span.to), part = src.slice(Math.max(from, span.from), Math.min(to, span.to));
			const rendered = cell.textContent ?? '';
			const inSource = occurrences(text, part);
			const index = inSource.indexOf(Math.max(from, span.from) - span.from);
			const found = occurrences(rendered, part);
			if (inside && index >= 0 && found.length === inSource.length && found[index] !== undefined) {
				const range = document.createRange();
				const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
				let seen = 0, startSet = false;
				for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
					const length = node.data.length;
					if (!startSet && found[index] < seen + length) { range.setStart(node, found[index] - seen); startSet = true; }
					if (startSet && found[index] + part.length <= seen + length) { range.setEnd(node, found[index] + part.length - seen); break; }
					seen += length;
				}
				if (startSet) { wrapRange(range, mark); continue; }
			}
			cell.classList.add(...markClasses(mark.kind));
			addCommentId(cell, mark);
			cell.dataset.commentAria = 'true';
			cell.setAttribute('aria-label', `Commented cell: ${clean(cell.textContent ?? '')}`);
		}
	}
}
