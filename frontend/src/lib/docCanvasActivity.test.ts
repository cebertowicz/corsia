import { describe, expect, it } from 'vitest';
import { UnseenDocActivity, type ActivityPosition } from './docCanvasActivity';
import type { DocActivityFrame } from './docPresence';

const frame = (id: string, from = 100): DocActivityFrame => ({
	type: 'doc_activity', artifact_id: 'doc', thread_id: 'agent', agent: 'Reviewer',
	kind: 'comment', span: { from, to: from + 10 }, comment_id: id, revision: null, at: id,
});
const position = (from: number): ActivityPosition => from < 100
	? { direction: 'above', distance: 20 }
	: { direction: 'below', distance: 20 };

describe('unseen document arrivals', () => {
	it('counts only the nearest direction, below wins a tie, and switches once seen', () => {
		const unseen = new UnseenDocActivity();
		unseen.add(frame('above', 20), position);
		unseen.add(frame('below-1', 100), position);
		unseen.add(frame('below-2', 200), position);
		expect(unseen.pill(position)).toMatchObject({ count: 2, direction: 'below', span: { from: 100, to: 110 } });
		unseen.seen((from) => from >= 100 ? { direction: 'visible', distance: 0 } : position(from));
		expect(unseen.pill(position)).toMatchObject({ count: 1, direction: 'above' });
	});
	it('never counts visible, unlocatable or duplicate activity', () => {
		const unseen = new UnseenDocActivity();
		const locate = (from: number): ActivityPosition | null => from === 0 ? null : from === 10 ? { direction: 'visible', distance: 0 } : position(from);
		unseen.add(frame('invalid', 0), locate);
		unseen.add(frame('visible', 10), locate);
		unseen.add(frame('new'), locate);
		unseen.add(frame('new'), locate);
		expect(unseen.pill(locate)?.count).toBe(1);
		unseen.seen(() => ({ direction: 'visible', distance: 0 }));
		unseen.add(frame('new'), locate);
		expect(unseen.pill(locate)).toBeNull();
	});
	it('uses nearest viewport-edge distance and removes invalid destinations', () => {
		const unseen = new UnseenDocActivity();
		const locate = (from: number): ActivityPosition => ({ direction: from < 100 ? 'above' : 'below', distance: from });
		unseen.add(frame('above', 20), locate);
		unseen.add(frame('below', 200), locate);
		expect(unseen.pill(locate)?.direction).toBe('above');
		unseen.seen(() => null);
		expect(unseen.pill(locate)).toBeNull();
	});
	it('maps unseen offsets through edits and clears its session on leaving', () => {
		const unseen = new UnseenDocActivity();
		unseen.add(frame('new'), position);
		unseen.map((offset) => offset + 20);
		expect(unseen.pill(position)?.span.from).toBe(120);
		unseen.clear();
		expect(unseen.pill(position)).toBeNull();
	});
});
