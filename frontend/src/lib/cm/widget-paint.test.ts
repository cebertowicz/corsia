import { afterEach, describe, expect, it } from 'vitest';
import { addCommentId, commentIdsAt, paintTable, unpaint, wrapRange, type Mark } from './widget-paint';

const SRC = '| Item | Owner |\n|---|---|\n| Alpha | Dana and Dana |\n| Bravo | Eli |';
const BASE = 20;

/** A drawn table widget for SRC: header row, then Alpha and Bravo (the delimiter row is not drawn). */
function widget() {
	const el = document.body.appendChild(document.createElement('div'));
	el.className = 'cm-md-table';
	el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>Item</th><th>Owner</th></tr></thead><tbody><tr><td>Alpha</td><td>Dana and <em>Dana</em></td></tr><tr><td>Bravo</td><td>Eli</td></tr></tbody></table></div>';
	return el;
}
const mark = (id: string, part: string, kind: Mark['kind'] = 'placed', nth = 0): Mark => {
	let at = -1;
	for (let seen = 0; seen <= nth; seen += 1) at = SRC.indexOf(part, at + 1);
	return { id, kind, from: BASE + at, to: BASE + at + part.length };
};
const cell = (el: HTMLElement, row: number, col: number) => el.querySelector('table')!.rows[row].cells[col];

afterEach(() => document.body.replaceChildren());

describe('paintTable and unpaint', () => {
	it('restore the widget exactly after painting a mark inside formatted cell text', () => {
		const el = widget(), before = el.innerHTML;
		paintTable(el, BASE, SRC, [mark('a', 'Dana', 'placed', 1)]);
		expect(el.querySelector('.cm-comment-wrap')?.textContent).toBe('Dana');
		expect(el.innerHTML).not.toBe(before);
		unpaint(el);
		expect(el.innerHTML).toBe(before);
	});

	it('restore the widget exactly after painting whole cells, several rows and the whole table', () => {
		const el = widget(), before = el.innerHTML;
		paintTable(el, BASE, SRC, [mark('a', 'Eli'), mark('b', 'Alpha | Dana and Dana', 'focused'), { id: 'c', kind: 'pending', from: BASE, to: BASE + SRC.length }]);
		expect(cell(el, 2, 1).classList.contains('cm-comment-anchor')).toBe(true);
		expect(el.classList.contains('cm-comment-block-pending')).toBe(true);
		unpaint(el);
		expect(el.innerHTML).toBe(before);
		expect(el.className).toBe('cm-md-table');
	});

	it('keeps every comment on one cell and on a whole table, in paint order, and never a pending mark', () => {
		const el = widget();
		paintTable(el, BASE, SRC, [mark('a', 'Eli'), mark('b', 'Eli'), { id: 'draft', kind: 'pending', from: BASE + SRC.indexOf('Eli'), to: BASE + SRC.indexOf('Eli') + 3 }]);
		expect(cell(el, 2, 1).dataset.commentIds).toBe('a,b');
		const whole = widget();
		paintTable(whole, BASE, SRC, [{ id: 'a', kind: 'placed', from: BASE, to: BASE + SRC.length }, { id: 'draft', kind: 'pending', from: BASE, to: BASE + SRC.length }, { id: 'b', kind: 'focused', from: BASE, to: BASE + SRC.length }]);
		expect(whole.dataset.commentIds).toBe('a,b');
		expect(whole.classList.contains('cm-comment-block-pending')).toBe(true);
	});
});

describe('commentIdsAt', () => {
	it('gathers the comments of every painted element between the click and the widget', () => {
		const el = widget();
		paintTable(el, BASE, SRC, [mark('a', 'Dana', 'placed', 1), mark('b', 'Dana', 'placed', 1), mark('c', 'Alpha | Dana and Dana')]);
		const inner = [...el.querySelectorAll('em .cm-comment-wrap')].pop()!;
		expect(commentIdsAt(el, inner).sort()).toEqual(['a', 'b', 'c']);
		expect(commentIdsAt(el, cell(el, 2, 0))).toEqual([]);
	});

	it('stops at the widget, and counts the widget itself', () => {
		const el = widget();
		el.dataset.commentIds = 'whole';
		document.body.dataset.commentIds = 'outside';
		expect(commentIdsAt(el, cell(el, 1, 0))).toEqual(['whole']);
		delete document.body.dataset.commentIds;
	});
});

describe('wrapRange', () => {
	it('wraps a range across formatting in one span per text node, and unpaint restores the DOM', () => {
		const el = widget(), before = el.innerHTML, target = cell(el, 1, 1);
		const range = document.createRange();
		range.setStart(target.firstChild!, 4);
		range.setEnd(target.querySelector('em')!.firstChild!, 2);
		wrapRange(range, { id: 'a', kind: 'focused', from: 0, to: 1 });
		const wraps = [...target.querySelectorAll<HTMLElement>('.cm-comment-wrap')];
		expect(wraps.map((wrap) => wrap.textContent)).toEqual([' and ', 'Da']);
		expect(wraps.every((wrap) => wrap.dataset.commentIds === 'a' && wrap.classList.contains('cm-comment-focused'))).toBe(true);
		unpaint(el);
		expect(el.innerHTML).toBe(before);
	});

	it('wraps nothing for a collapsed range', () => {
		const el = widget(), before = el.innerHTML, range = document.createRange();
		range.setStart(cell(el, 1, 0).firstChild!, 2);
		range.setEnd(cell(el, 1, 0).firstChild!, 2);
		wrapRange(range, { id: 'a', kind: 'placed', from: 0, to: 0 });
		expect(el.innerHTML).toBe(before);
	});
});

describe('addCommentId', () => {
	it('does not repeat an id', () => {
		const el = document.createElement('div');
		addCommentId(el, { id: 'a', kind: 'placed', from: 0, to: 1 });
		addCommentId(el, { id: 'a', kind: 'focused', from: 0, to: 1 });
		expect(el.dataset.commentIds).toBe('a');
	});
});
