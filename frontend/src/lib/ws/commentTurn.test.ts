import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import { commentLines, commentTurn, createCommentTurn, IDLE_MS, RENEW_MS, type CommentTurnEnv, type CommentTurnFrame } from './commentTurn';
import { emit } from './events';

type Timer = { at: number; fn: () => void; live: boolean };

function fakeEnv() {
	let clock = 1_000_000;
	let visible = true;
	let lastInput = clock;
	const timers: Timer[] = [];
	const sent: CommentTurnFrame[] = [];
	const left: string[] = [];
	const env: CommentTurnEnv = {
		send: (frame) => { sent.push(frame); },
		now: () => clock,
		lastInputAt: () => lastInput,
		isVisible: () => visible,
		setTimer: (fn, ms) => { const timer = { at: clock + ms, fn, live: true }; timers.push(timer); return timer; },
		clearTimer: (handle) => { (handle as Timer).live = false; },
		left: (id) => { left.push(id); },
	};
	const advance = (ms: number) => {
		const end = clock + ms;
		for (;;) {
			const next = timers.filter((t) => t.live && t.at <= end).sort((a, b) => a.at - b.at)[0];
			if (!next) break;
			clock = next.at;
			next.live = false;
			next.fn();
		}
		clock = end;
	};
	return {
		env, sent, left, advance,
		touch: () => { lastInput = clock; },
		hide: () => { visible = false; },
		show: () => { visible = true; },
	};
}

const ON = (id: string): CommentTurnFrame => ({ type: 'comment_turn', on: true, comment_id: id });
const OFF: CommentTurnFrame = { type: 'comment_turn', on: false };

describe('comment writer turn', () => {
	it('selecting a card takes the turn and moving to another releases it first', () => {
		const f = fakeEnv(), turn = createCommentTurn(f.env), list = {};
		turn.show(list, 'a');
		turn.show(list, 'b');
		expect(f.sent).toEqual([ON('a'), OFF, ON('b')]);
		expect(f.left).toEqual(['a']);
		expect(turn.held).toBe('b');
	});

	it('renews once a minute while there is input, and releases after 2 minutes idle', () => {
		const f = fakeEnv(), turn = createCommentTurn(f.env), list = {};
		turn.show(list, 'a');
		f.advance(RENEW_MS);
		expect(f.sent).toEqual([ON('a'), ON('a')]);
		f.advance(IDLE_MS - RENEW_MS);
		expect(f.sent.at(-1)).toEqual(OFF);
		expect(turn.held).toBeNull();
		f.advance(5 * RENEW_MS);
		expect(f.sent).toHaveLength(3);
		f.touch();
		turn.input();
		expect(f.sent.at(-1)).toEqual(ON('a'));
	});

	it('send or clear releases; only typing or a new selection takes it back', () => {
		const f = fakeEnv(), turn = createCommentTurn(f.env), list = {};
		turn.show(list, 'a');
		turn.draft(list, false);
		turn.draft(list, true);
		turn.draft(list, false);
		expect(f.sent).toEqual([ON('a'), OFF]);
		turn.input();
		expect(f.sent).toHaveLength(2);
		turn.draft(list, true);
		expect(f.sent.at(-1)).toEqual(ON('a'));
	});

	it('a hidden tab releases and coming back with input takes it again', () => {
		const f = fakeEnv(), turn = createCommentTurn(f.env), list = {};
		turn.show(list, 'a');
		f.hide();
		turn.visibilityChanged();
		expect(f.sent.at(-1)).toEqual(OFF);
		f.show();
		turn.visibilityChanged();
		expect(f.sent.at(-1)).toEqual(ON('a'));
	});

	it('closing the list releases, and another list closing does not', () => {
		const f = fakeEnv(), turn = createCommentTurn(f.env), list = {}, other = {};
		turn.show(list, 'a');
		turn.show(other, null);
		turn.close(other);
		expect(turn.held).toBe('a');
		turn.close(list);
		expect(f.sent).toEqual([ON('a'), OFF]);
	});

	it('a reconnect re-sends the held turn at once', () => {
		const f = fakeEnv(), turn = createCommentTurn(f.env), list = {};
		turn.show(list, 'a');
		turn.reconnected();
		expect(f.sent).toEqual([ON('a'), ON('a')]);
	});

	it('comment_line frames set and clear the waiting count', () => {
		commentTurn();
		emit('comment_line', { type: 'comment_line', artifact_id: 'd', comment_id: 'r1', waiting: 2 });
		expect(get(commentLines)).toEqual({ r1: 2 });
		emit('comment_line', { type: 'comment_line', artifact_id: 'd', comment_id: 'r1', waiting: 0 });
		expect(get(commentLines)).toEqual({});
	});
});
