import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { beginComposing, cancelComposing, mergeInView, noteComposerText, setVisibleRows, takeReplyContext } from './replyContextCapture';
import { visitDividers } from './threadUpdateCards';

function rows(threadId: string, ...seqs: number[]) {
	return seqs.map((seq) => ({ threadId, seq }));
}

beforeEach(() => {
	cancelComposing();
	setVisibleRows(rows('t1'));
	visitDividers.set({});
});

afterEach(() => {
	cancelComposing();
	setVisibleRows(rows('t1'));
	visitDividers.set({});
});

describe('mergeInView', () => {
	it('joins overlapping and touching spans and counts only the rows seen', () => {
		const seen = new Set([3, 4, 5, 6, 20, 21, 22]);
		expect(mergeInView([[20, 22], [3, 5], [6, 6]], seen)).toEqual([[3, 6, 4], [20, 22, 3]]);
	});

	it('keeps a span that crosses a collapsed block wide, with only the visible rows counted', () => {
		expect(mergeInView([[10, 2010]], new Set([10, 11, 2010]))).toEqual([[10, 2010, 3]]);
	});

	it('returns nothing for nothing seen', () => {
		expect(mergeInView([], new Set())).toEqual([]);
	});
});

describe('reply context capture', () => {
	it('accumulates every row that was on screen at any moment while composing', () => {
		setVisibleRows(rows('t1', 40, 41, 42));
		beginComposing('t1');
		setVisibleRows(rows('t1', 20, 21));
		setVisibleRows(rows('t1', 21, 22, 23));
		expect(takeReplyContext('t1')).toEqual({ in_view: [[20, 23, 4], [40, 42, 3]] });
	});

	it('counts the rows already on screen when the first input arrives', () => {
		setVisibleRows(rows('t1', 7, 8));
		beginComposing('t1');
		expect(takeReplyContext('t1')).toEqual({ in_view: [[7, 8, 2]] });
	});

	it('ignores rows seen before composing began', () => {
		setVisibleRows(rows('t1', 1, 2));
		setVisibleRows(rows('t1', 30));
		beginComposing('t1');
		setVisibleRows(rows('t1', 31));
		expect(takeReplyContext('t1')).toEqual({ in_view: [[30, 31, 2]] });
	});

	it('starts a fresh window after a send', () => {
		beginComposing('t1');
		setVisibleRows(rows('t1', 5));
		expect(takeReplyContext('t1')).toEqual({ in_view: [[5, 5, 1]] });
		setVisibleRows(rows('t1', 9));
		expect(takeReplyContext('t1')).toBeNull();
	});

	it('does not carry a window into another thread', () => {
		beginComposing('t1');
		setVisibleRows(rows('t1', 5));
		expect(takeReplyContext('t2')).toBeNull();
		expect(takeReplyContext('t1')).toBeNull();
	});

	it('never counts rows of another thread, whether seen before or during composing', () => {
		setVisibleRows(rows('t2', 90, 91));
		beginComposing('t1');
		setVisibleRows([...rows('t1', 5), ...rows('t2', 92)]);
		// Another thread's composer is empty, so it never cancels this window.
		noteComposerText('t2', '');
		expect(takeReplyContext('t1')).toEqual({ in_view: [[5, 5, 1]] });
	});

	it('forgets a draft that was emptied without sending', () => {
		beginComposing('t1');
		setVisibleRows(rows('t1', 5));
		cancelComposing();
		setVisibleRows(rows('t1'));
		beginComposing('t1');
		setVisibleRows(rows('t1', 9));
		expect(takeReplyContext('t1')).toEqual({ in_view: [[9, 9, 1]] });
	});

	it('opens on the first typed character and closes when its own composer is emptied', () => {
		setVisibleRows(rows('t1', 5));
		noteComposerText('t1', '   ');
		noteComposerText('t1', 'y');
		setVisibleRows(rows('t1', 6));
		noteComposerText('t2', '');
		noteComposerText('t1', '');
		noteComposerText('t1', 'yes');
		expect(takeReplyContext('t1')).toEqual({ in_view: [[6, 6, 1]] });
	});

	it('an empty composer for another thread does not end this window', () => {
		setVisibleRows(rows('t1', 5));
		noteComposerText('t1', 'y');
		noteComposerText('t2', '');
		expect(takeReplyContext('t1')).toEqual({ in_view: [[5, 5, 1]] });
	});

	it('beginning twice for one thread keeps the window', () => {
		beginComposing('t1');
		setVisibleRows(rows('t1', 5));
		beginComposing('t1');
		expect(takeReplyContext('t1')).toEqual({ in_view: [[5, 5, 1]] });
	});

	it('reads the first unread message from this visit divider, with truncation', () => {
		visitDividers.set({
			t1: { divider: { since: 's', first_seq: 12, truncated: true }, failed: false },
			t2: { divider: { since: 's', first_seq: 3 }, failed: false },
			t3: { divider: { since: 's', first_seq: null }, failed: false },
			t4: { divider: null, failed: true },
		});
		expect(takeReplyContext('t1')).toEqual({ first_unread: { seq: 12, truncated: true } });
		expect(takeReplyContext('t2')).toEqual({ first_unread: { seq: 3, truncated: false } });
		expect(takeReplyContext('t3')).toBeNull();
		expect(takeReplyContext('t4')).toBeNull();
		expect(takeReplyContext('t5')).toBeNull();
	});

	it('carries both when both exist', () => {
		visitDividers.set({ t1: { divider: { since: 's', first_seq: 2 }, failed: false } });
		beginComposing('t1');
		setVisibleRows(rows('t1', 6));
		expect(takeReplyContext('t1')).toEqual({
			in_view: [[6, 6, 1]], first_unread: { seq: 2, truncated: false },
		});
	});
});
