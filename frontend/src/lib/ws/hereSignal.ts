/**
 * The app-wide "here" signal (urgent notifications, R4): tells the server over the existing socket whether the person is
 * using CORSIA right now, on any view. It is its own message, not a thread claim: it never names a thread and never
 * changes what the server counts as read or seen.
 *
 * Here = the page is visible and had input in the last 10 minutes (today's `IDLE_CAP_MS`). A hidden page sends away with
 * `hidden: true`; the server keeps counting it here for its existing 30 s hidden grace. Frames carry state only, are sent
 * on change, and the current state is sent again on every reconnect (a new socket starts away). No input, no frames.
 */
import { connectionState } from '../stores/connection';
import { lastUserInputAt, onUserInput } from '../userInput';
import { IDLE_CAP_MS } from './presence';
import { ws } from './state';

export type HereFrame = { type: 'here'; state: 'here' | 'away'; hidden?: true };

export interface HereEnv {
	send(frame: HereFrame): void;
	now(): number;
	lastInputAt(): number;
	isVisible(): boolean;
	setTimer(fn: () => void, ms: number): unknown;
	clearTimer(handle: unknown): void;
}

export interface HereSignal {
	/** Visibility or input changed: re-read both. */
	update(): void;
	/** A new socket authenticated: tell it where things stand. */
	reconnected(): void;
}

export function createHereSignal(env: HereEnv): HereSignal {
	let sent: 'here' | 'away' = 'away';
	let idleTimer: unknown = null;

	const recent = () => env.now() - env.lastInputAt() < IDLE_CAP_MS;

	function arm() {
		if (idleTimer !== null) env.clearTimer(idleTimer);
		idleTimer = null;
		if (sent === 'here') idleTimer = env.setTimer(update, Math.max(0, env.lastInputAt() + IDLE_CAP_MS - env.now()));
	}

	function update() {
		idleTimer = null;
		const visible = env.isVisible();
		if (visible && recent()) {
			if (sent !== 'here') env.send({ type: 'here', state: 'here' });
			sent = 'here';
		} else if (sent === 'here') {
			// Leaving: a hidden page says so, so the server applies its grace; an idle one is simply away.
			env.send(visible ? { type: 'here', state: 'away' } : { type: 'here', state: 'away', hidden: true });
			sent = 'away';
		}
		arm();
	}

	return {
		update,
		reconnected() {
			if (sent === 'here') env.send({ type: 'here', state: 'here' });
		},
	};
}

let browserSignal: HereSignal | null = null;

/** Start the app-wide signal for this page once; safe to call again. */
export function startHereSignal(): void {
	if (browserSignal || typeof document === 'undefined' || typeof window === 'undefined') return;
	const signal = createHereSignal({
		send(frame) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame)); },
		now: () => Date.now(),
		lastInputAt: lastUserInputAt,
		isVisible: () => document.visibilityState === 'visible',
		setTimer: (fn, ms) => setTimeout(fn, ms),
		clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
	});
	document.addEventListener('visibilitychange', () => signal.update());
	onUserInput(() => signal.update());
	connectionState.subscribe((state) => { if (state === 'connected') { signal.reconnected(); signal.update(); } });
	browserSignal = signal;
	signal.update();
}
