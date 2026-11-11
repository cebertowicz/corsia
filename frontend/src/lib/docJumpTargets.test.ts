import { afterEach, describe, expect, it } from 'vitest';
import { docJumpTargets, jumpedThread, jumpTargetLabel } from './docJumpTargets';
import type { DocComment } from '$lib/stores/docComments';

/** The editor's scrollport as the control reads it: what sits between its top and bottom is on screen. */
const VIEW = { top: 100, bottom: 500 };

const box = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 400, x: 0, y: top, width: 400, height: bottom - top }) as DOMRect;
const paint = (el: HTMLElement, top: number, bottom: number) => { el.getBoundingClientRect = () => box(top, bottom); };

const thread = (id: string, extra: Partial<DocComment> = {}): DocComment =>
	({ id, anchor: `quote on ${id}`, author: 'agent-a', authorKind: 'agent', text: 'ask', at: '', resolved: false, replies: [], newReplyIds: [`${id}-r1`], ...extra });

let roots: HTMLElement[] = [];
afterEach(() => { roots.forEach((el) => el.remove()); roots = []; });

/** A document canvas reduced to what the control reads: one scrollport and the highlights painted inside it. */
function canvas(top = VIEW.top, bottom = VIEW.bottom): HTMLElement {
	const root = document.createElement('div');
	root.innerHTML = '<div class="cm-scroller"><div class="cm-content"></div></div>';
	paint(root, top, bottom);
	paint(root.querySelector<HTMLElement>('.cm-scroller')!, top, bottom);
	document.body.appendChild(root);
	roots.push(root);
	return root;
}

function mark(canvasEl: HTMLElement, id: string, top: number, bottom: number): HTMLElement {
	const el = document.createElement('span');
	el.dataset.commentIds = id;
	paint(el, top, bottom);
	canvasEl.querySelector<HTMLElement>('.cm-content')!.appendChild(el);
	return el;
}

function marginColumn(canvasEl: HTMLElement, cards: Record<string, { top: number; bottom: number; placed?: boolean }>): void {
	const column = document.createElement('section');
	column.className = 'drawer-comments';
	paint(column, VIEW.top, VIEW.bottom);
	const stack = document.createElement('div');
	stack.className = 'margin-stack';
	for (const [id, card] of Object.entries(cards)) {
		const el = document.createElement('div');
		el.className = `margin-card${card.placed === false ? '' : ' placed'}`;
		el.dataset.marginCard = id;
		paint(el, card.top, card.bottom);
		stack.appendChild(el);
	}
	column.appendChild(stack);
	canvasEl.appendChild(column);
}

describe('docJumpTargets', () => {
	it('points below at the nearest thread whose text is off screen, and counts every unread answer', () => {
		const canvasEl = canvas();
		mark(canvasEl, 'far', 900, 940);
		mark(canvasEl, 'near', 560, 600);
		const result = docJumpTargets(canvasEl, [thread('far'), thread('near')]);
		expect(result).toEqual({ count: 2, target: { id: 'near', label: '“quote on near”', distance: 60 }, direction: 'below' });
	});

	it('points above at the nearest thread above, and above when the two sides sit even', () => {
		const canvasEl = canvas();
		mark(canvasEl, 'high', 0, 90);
		mark(canvasEl, 'low', 540, 580);
		const above = docJumpTargets(canvasEl, [thread('high'), thread('low')]);
		expect(above.target?.id).toBe('high');
		expect(above.direction).toBe('above');

		const evenEl = canvas();
		mark(evenEl, 'up', 60, 100);
		mark(evenEl, 'down', 500, 540);
		const even = docJumpTargets(evenEl, [thread('up'), thread('down')]);
		expect(even.direction).toBe('above');
		expect(even.target?.id).toBe('up');
	});

	it('is no target at all while every unread thread has a line of its text on screen', () => {
		const canvasEl = canvas();
		mark(canvasEl, 'one', 200, 240);
		mark(canvasEl, 'two', 300, 340);
		expect(docJumpTargets(canvasEl, [thread('one'), thread('two')])).toEqual({ count: 2, target: null, direction: null });
	});

	it('treats a highlight that spans the fold as on screen, and one the reader scrolled past as above', () => {
		const canvasEl = canvas();
		mark(canvasEl, 'spans', 80, 120);
		expect(docJumpTargets(canvasEl, [thread('spans')]).target).toBeNull();
		paint(canvasEl.querySelector<HTMLElement>('[data-comment-ids]')!, 20, 80);
		const scrolled = docJumpTargets(canvasEl, [thread('spans')]);
		expect(scrolled).toMatchObject({ direction: 'above', target: { id: 'spans', distance: 20 } });
	});

	it('is still a target when the editor has drawn nothing for it, below when nothing is drawn either side', () => {
		const canvasEl = canvas();
		const result = docJumpTargets(canvasEl, [thread('unrendered', { from: 9_000 })]);
		expect(result).toEqual({ count: 1, target: { id: 'unrendered', label: '“quote on unrendered”', distance: 0 }, direction: 'below' });
	});

	it('takes the margin card as the landing when there is no highlight to paint', () => {
		const canvasEl = canvas();
		marginColumn(canvasEl, { gone: { top: 620, bottom: 700 } });
		const result = docJumpTargets(canvasEl, [thread('gone', { detached: true, orphaned: true })]);
		expect(result).toMatchObject({ direction: 'below', target: { id: 'gone', distance: 120 } });
	});

	it('ignores a card that is not placed yet, and counts its thread as undrawn', () => {
		const canvasEl = canvas();
		marginColumn(canvasEl, { hidden: { top: 620, bottom: 700, placed: false } });
		expect(docJumpTargets(canvasEl, [thread('hidden')])).toMatchObject({ direction: 'below', target: { id: 'hidden', distance: 0 } });
	});

	it('counts a suggestion and its answers beside a plain thread, and stops counting a decided one that has nothing unread', () => {
		const canvasEl = canvas();
		mark(canvasEl, 'sug', 900, 940);
		const result = docJumpTargets(canvasEl, [
			thread('sug', { replacement: 'so it reads kindly', newReplyIds: ['a', 'b'] }),
			thread('ask', { newComment: true, newReplyIds: [] }),
			thread('done', { replacement: 'x', decision: 'accepted', newReplyIds: [] }),
		]);
		expect(result.count).toBe(3);
		expect(result.target?.id).toBe('sug');
	});

	it('names a comment with no quote by its pin on the image', () => {
		const canvasEl = canvas();
		const tag = document.createElement('button');
		tag.dataset.regionTag = '3';
		tag.dataset.commentId = 'pin';
		paint(tag, 700, 730);
		canvasEl.appendChild(tag);
		const result = docJumpTargets(canvasEl, [thread('pin', { anchor: '', regionNumber: 3 })]);
		expect(result).toMatchObject({ direction: 'below', target: { id: 'pin', label: 'pin 3' } });
	});

	it("reads a pin in an open image against the image's viewport, on all four sides", () => {
		const root = document.createElement('div');
		root.innerHTML = '<div class="viewport"></div>';
		// The viewer's toolbar sits above its viewport, so the root reaches higher than what shows the image.
		paint(root, 60, VIEW.bottom);
		const viewport = root.querySelector<HTMLElement>('.viewport')!;
		viewport.getBoundingClientRect = () => ({ top: VIEW.top, bottom: VIEW.bottom, left: 0, right: 400 }) as DOMRect;
		const pin = (id: string, top: number, left: number) => {
			const tag = document.createElement('button');
			tag.dataset.regionTag = id;
			tag.dataset.commentId = id;
			tag.getBoundingClientRect = () => ({ top, bottom: top + 20, left, right: left + 20, width: 20, height: 20 }) as DOMRect;
			viewport.appendChild(tag);
		};
		document.body.appendChild(root);
		roots.push(root);
		const unread = (id: string) => thread(id, { anchor: '', regionNumber: 1 });

		pin('shown', 300, 100);
		expect(docJumpTargets(root, [unread('shown')]).target).toBeNull();
		pin('right', 380, 450);
		expect(docJumpTargets(root, [unread('right')])).toMatchObject({ direction: 'below', target: { id: 'right', distance: 50 } });
		pin('left', 120, -60);
		expect(docJumpTargets(root, [unread('left')])).toMatchObject({ direction: 'above', target: { id: 'left', distance: 40 } });
		pin('toolbar', 70, 100);
		expect(docJumpTargets(root, [unread('toolbar')])).toMatchObject({ direction: 'above', target: { id: 'toolbar', distance: 10 } });
	});

	it('gives the thread the tap goes to out of the same list', () => {
		const threads = [thread('a'), thread('b')];
		const canvasEl = canvas();
		mark(canvasEl, 'b', 800, 820);
		const { target } = docJumpTargets(canvasEl, threads);
		expect(jumpedThread(threads, target)?.id).toBe('b');
		expect(jumpedThread(threads, null)).toBeNull();
	});
});

describe('jumpTargetLabel', () => {
	it("reads the quote as a card's label does: markdown marks out, whitespace flattened, cut to a snippet", () => {
		expect(jumpTargetLabel({ anchor: 'The **lead** signs\noff by eight', regionNumber: null })).toBe('“The lead signs off by eight”');
		expect(jumpTargetLabel({ anchor: 'x'.repeat(80), regionNumber: null })).toBe(`“${'x'.repeat(64)}…”`);
	});

	it('names a thread with nothing to quote by its pin, or as unmarked', () => {
		expect(jumpTargetLabel({ anchor: '', regionNumber: 2 })).toBe('pin 2');
		expect(jumpTargetLabel({ anchor: '', regionNumber: null })).toBe('a comment with no text to mark');
	});
});
