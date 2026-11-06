/**
 * A comment thread's writer turn, held by the person while they are on it (DRAFT-comment-lock).
 *
 * The comment list reports the thread whose card is selected (`show`) and whether its reply box
 * has text (`draft`). While the person is on that thread this page holds its turn: it sends
 * `{type: 'comment_turn', comment_id, on: true}` once, then again each minute while there has
 * been input in the last 2 minutes. It sends `{on: false}` when the person selects another thread
 * or none, sends or clears the reply, hides the tab, or goes 2 minutes without input. After a
 * send or clear the turn comes back only by typing in the reply box or selecting a card again.
 * The server drops a turn that is not renewed, and a closed socket's turn at once.
 *
 * The server answers on the thread the person is on with `comment_line` frames: how many agent
 * writes wait for the turn. `commentLines` holds them for the card.
 */
import { writable } from 'svelte/store';
import { connectionState } from '../stores/connection';
import { lastUserInputAt, onUserInput } from '../userInput';
import { on } from './events';
import { ws } from './state';

export const RENEW_MS = 60_000;
export const IDLE_MS = 2 * 60_000;

export type CommentTurnFrame = { type: 'comment_turn'; on: true; comment_id: string } | { type: 'comment_turn'; on: false };

export interface CommentTurnEnv {
	send(frame: CommentTurnFrame): void;
	now(): number;
	/** The page input clock's last input; -Infinity before any. */
	lastInputAt(): number;
	isVisible(): boolean;
	setTimer(fn: () => void, ms: number): unknown;
	clearTimer(handle: unknown): void;
	/** The person stopped holding `commentId`: its waiting count no longer applies. */
	left(commentId: string): void;
}

export interface CommentTurn {
	/** `owner`'s list now has `commentId` selected, or none. */
	show(owner: object, commentId: string | null): void;
	/** The selected thread's reply box has text, or not. */
	draft(owner: object, hasText: boolean): void;
	/** `owner`'s list is gone. */
	close(owner: object): void;
	visibilityChanged(): void;
	input(): void;
	reconnected(): void;
	readonly held: string | null;
}

export function createCommentTurn(env: CommentTurnEnv): CommentTurn {
	let owner: object | null = null;
	let target: string | null = null;
	let held = false;
	/** Sent or cleared: only typing or a new selection takes the turn back. */
	let settled = false;
	let hadText: boolean | null = null;
	let renewTimer: unknown = null;
	let idleTimer: unknown = null;

	const clear = () => {
		if (renewTimer !== null) env.clearTimer(renewTimer);
		if (idleTimer !== null) env.clearTimer(idleTimer);
		renewTimer = idleTimer = null;
	};
	const recent = () => env.now() - env.lastInputAt() < IDLE_MS;

	function armIdle() {
		if (idleTimer !== null) env.clearTimer(idleTimer);
		idleTimer = env.setTimer(() => {
			idleTimer = null;
			if (recent()) armIdle(); else release();
		}, Math.max(0, env.lastInputAt() + IDLE_MS - env.now()));
	}

	function renew() {
		renewTimer = null;
		if (!held || !target) return;
		env.send({ type: 'comment_turn', on: true, comment_id: target });
		renewTimer = env.setTimer(renew, RENEW_MS);
	}

	function take() {
		if (held || !target || settled || !env.isVisible() || !recent()) return;
		held = true;
		renew();
		armIdle();
	}

	function release() {
		clear();
		if (!held) return;
		held = false;
		env.send({ type: 'comment_turn', on: false });
		if (target) env.left(target);
	}

	return {
		show(by, commentId) {
			if (owner !== null && owner !== by && commentId === null) return;
			if (owner === by && target === commentId) return;
			release();
			owner = commentId === null ? null : by;
			target = commentId;
			settled = false;
			hadText = null;
			take();
		},
		draft(by, hasText) {
			if (by !== owner || !target) return;
			const before = hadText;
			hadText = hasText;
			if (before === null || before === hasText) return;
			if (hasText) { settled = false; take(); } else { release(); settled = true; }
		},
		close(by) {
			if (by !== owner) return;
			release();
			owner = target = hadText = null;
		},
		visibilityChanged() {
			if (env.isVisible()) take(); else release();
		},
		input() {
			if (held) armIdle(); else take();
		},
		reconnected() {
			if (!held) return;
			if (renewTimer !== null) env.clearTimer(renewTimer);
			renew();
		},
		get held() {
			return held ? target : null;
		},
	};
}

/** Agent writes waiting for the turn, by thread root, on threads this page holds. */
export const commentLines = writable<Record<string, number>>({});

let browserTurn: CommentTurn | null = null;

function forget(commentId: string) {
	commentLines.update((lines) => {
		if (!(commentId in lines)) return lines;
		const { [commentId]: _gone, ...rest } = lines;
		return rest;
	});
}

/** This page's writer turn; null outside a browser. */
export function commentTurn(): CommentTurn | null {
	if (browserTurn) return browserTurn;
	if (typeof document === 'undefined' || typeof window === 'undefined') return null;
	const turn = createCommentTurn({
		send(frame) {
			if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
		},
		now: () => Date.now(),
		lastInputAt: lastUserInputAt,
		isVisible: () => document.visibilityState === 'visible',
		setTimer: (fn, ms) => setTimeout(fn, ms),
		clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
		left: forget,
	});
	document.addEventListener('visibilitychange', () => turn.visibilityChanged());
	onUserInput(() => turn.input());
	connectionState.subscribe((stateNow) => {
		if (stateNow === 'connected') turn.reconnected();
	});
	on('comment_line', (frame: { comment_id?: unknown; waiting?: unknown }) => {
		if (typeof frame?.comment_id !== 'string' || typeof frame.waiting !== 'number') return;
		const id = frame.comment_id, waiting = frame.waiting;
		if (waiting <= 0) forget(id);
		else commentLines.update((lines) => ({ ...lines, [id]: waiting }));
	});
	browserTurn = turn;
	return turn;
}
