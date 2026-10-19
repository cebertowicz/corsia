import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as productEvents from '../product_events';
import { connectionState } from '../stores/connection';
import {
	createPresenceTracker,
	DWELL_MS,
	IDLE_CAP_MS,
	startPresence,
	displayedDocument,
	getPresenceTracker,
	updateDocumentOpenCapability,
	stopPresence,
	type PresenceEnv,
	type PresenceFrame,
	type PresenceTracker,
} from './presence';
import { setWs } from './state';
import { get } from 'svelte/store';
import { NO_DOCUMENT_OPEN } from '../artifactDocumentOpenCapability';

type Timer = { at: number; fn: () => void; live: boolean };

function fakeEnv() {
	let clock = 1_000_000;
	let visible = true;
	let focused = true;
	let lastInput = -Infinity;
	const timers: Timer[] = [];
	const sent: PresenceFrame[] = [];
	const env: PresenceEnv = {
		send: (frame) => { sent.push(frame); },
		now: () => clock,
		lastInputAt: () => lastInput,
		isVisible: () => visible,
		isFocused: () => focused,
		setTimer: (fn, ms) => {
			const timer = { at: clock + ms, fn, live: true };
			timers.push(timer);
			return timer;
		},
		clearTimer: (handle) => { (handle as Timer).live = false; },
	};
	const advance = (ms: number) => {
		const end = clock + ms;
		for (;;) {
			const due = timers
				.filter((t) => t.live && t.at <= end)
				.sort((a, b) => a.at - b.at)[0];
			if (!due) break;
			clock = due.at;
			due.live = false;
			due.fn();
		}
		clock = end;
	};
	return {
		env, sent, advance,
		/** The user acts now: the page clock moves and tells the tracker. */
		input: (tracker: PresenceTracker) => { lastInput = clock; tracker.input(); },
		/** Input the page clock saw before any tracker existed. */
		inputEarlier: () => { lastInput = clock; },
		setVisible: (value: boolean) => { visible = value; },
		setFocused: (value: boolean) => { focused = value; },
	};
}

const states = (sent: PresenceFrame[]) => sent.map((frame) => frame.state);

describe('presence claim computation', () => {
	it('claims only after the 3 s dwell in a visible page with recent input', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS - 1);
		expect(sent).toEqual([]);
		advance(1);
		expect(sent).toEqual([
			{ type: 'presence', surface: 'chat', thread_id: 't1', state: 'foreground', focused: true },
		]);
	});

	it('sends document-open capability only when it changes, separate from the claim', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		tracker.updateDocumentOpenCapability({ canShow: false, showing: null });
		advance(DWELL_MS);
		expect(sent).toEqual([
			{ type: 'presence', surface: 'chat', thread_id: 't1', state: 'foreground', focused: true },
			{
				type: 'presence', surface: 'chat', thread_id: 't1', state: 'document_open_capability',
				document_open: { can_show: false, visible_artifact_id: null },
			},
		]);
		tracker.updateDocumentOpenCapability({ canShow: false, showing: null });
		expect(sent).toHaveLength(2);
		tracker.updateDocumentOpenCapability({ canShow: false, showing: 'artifact-a' });
		expect(sent).toHaveLength(3);
	});

	it('exposes the claimed thread only while its claim is held', () => {
		const { env, advance, input, setVisible } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		expect(tracker.claimedThreadId).toBe('t1');
		setVisible(false);
		tracker.visibilityChanged();
		expect(tracker.claimedThreadId).toBeNull();
	});

	it('does not claim a thread opened for under 3 s', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		for (const id of ['a', 'b', 'c', 'd', 'e']) {
			tracker.show(id);
			advance(1_000);
		}
		tracker.hide();
		advance(10_000);
		expect(sent).toEqual([]);
	});

	it('does not claim without input in the last 10 min', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		tracker.show('t1');
		advance(DWELL_MS * 2);
		expect(sent).toEqual([]);
		input(tracker);
		expect(states(sent)).toEqual(['foreground']);
	});

	it('ends and restarts a Measurement interval when focus leaves and returns', () => {
		const { env, sent, advance, input, setFocused } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		setFocused(false);
		tracker.focusChanged();
		setFocused(true);
		tracker.focusChanged();
		expect(sent).toEqual([
			{ type: 'presence', surface: 'chat', thread_id: 't1', state: 'foreground', focused: true },
			{ type: 'presence', surface: 'chat', thread_id: 't1', state: 'foreground', focused: false },
			{ type: 'presence', surface: 'chat', thread_id: 't1', state: 'foreground', focused: true },
		]);
	});

	it('sends background tagged hidden when the page is hidden, then re-dwells', () => {
		const { env, sent, advance, input, setVisible } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		setVisible(false);
		tracker.visibilityChanged();
		expect(sent.at(-1)).toEqual({
			type: 'presence', surface: 'chat', thread_id: 't1', state: 'background', reason: 'hidden',
		});
		setVisible(true);
		tracker.visibilityChanged();
		advance(DWELL_MS - 1);
		expect(states(sent)).toEqual(['foreground', 'background']);
		advance(1);
		expect(states(sent)).toEqual(['foreground', 'background', 'foreground']);
	});

	it('does not claim while the page is hidden', () => {
		const { env, sent, advance, input, setVisible } = fakeEnv();
		setVisible(false);
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS * 3);
		expect(sent).toEqual([]);
	});

	it('navigating away sends background without the hidden tag', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		tracker.show('t2');
		expect(sent.at(-1)).toEqual({
			type: 'presence', surface: 'chat', thread_id: 't1', state: 'background',
		});
		advance(DWELL_MS);
		expect(sent.at(-1)).toMatchObject({ thread_id: 't2', state: 'foreground' });
	});

	it('a view that stops showing threads (documents, settings) releases and never claims', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		tracker.hide();
		input(tracker);
		advance(IDLE_CAP_MS);
		expect(states(sent)).toEqual(['foreground', 'background']);
	});

	it('sends idle with idle_for_ms after 10 min without input, and re-claims on input', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		advance(IDLE_CAP_MS);
		expect(sent.at(-1)).toEqual({
			type: 'presence', surface: 'chat', thread_id: 't1', state: 'idle', idle_for_ms: IDLE_CAP_MS,
		});
		expect(tracker.claimed).toBe(false);
		input(tracker);
		expect(states(sent)).toEqual(['foreground', 'idle', 'foreground']);
	});

	it('input while claimed pushes the idle deadline back', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		advance(IDLE_CAP_MS - DWELL_MS - 1);
		input(tracker);
		advance(IDLE_CAP_MS - 1);
		expect(states(sent)).toEqual(['foreground']);
	});

	it('claims on input the page clock saw before the tracker existed', () => {
		const { env, sent, advance, inputEarlier } = fakeEnv();
		inputEarlier();
		const tracker = createPresenceTracker(env);
		tracker.show('t1');
		advance(DWELL_MS);
		expect(states(sent)).toEqual(['foreground']);
	});

	it('re-sends the current claim on reconnect, and nothing when unclaimed', () => {
		const { env, sent, advance, input } = fakeEnv();
		const tracker = createPresenceTracker(env);
		tracker.reconnected();
		expect(sent).toEqual([]);
		input(tracker);
		tracker.show('t1');
		advance(DWELL_MS);
		tracker.reconnected();
		expect(states(sent)).toEqual(['foreground', 'foreground']);
	});
});

describe('browser binding', () => {
	const spy = vi.spyOn(productEvents, 'emitProductEvent');
	const socket = { readyState: 1, send: vi.fn() };

	beforeEach(() => {
		vi.useFakeTimers();
		vi.stubGlobal('WebSocket', { OPEN: 1 });
		setWs(socket as unknown as WebSocket);
		socket.send.mockClear();
		spy.mockClear();
	});

	afterEach(() => {
		stopPresence();
		setWs(null);
		vi.unstubAllGlobals();
		vi.useRealTimers();
	});

	it('sends frames on the socket and never emits a product event', () => {
		window.dispatchEvent(new Event('pointerdown'));
		startPresence('t9');
		window.dispatchEvent(new Event('pointerdown'));
		vi.advanceTimersByTime(DWELL_MS);
		connectionState.set('connected');
		stopPresence();

		const frames = socket.send.mock.calls.map(([raw]) => JSON.parse(raw as string));
		expect(frames.map((frame) => frame.state)).toEqual(['foreground', 'foreground', 'background']);
		expect(frames.every((frame) => frame.thread_id === 't9' && frame.surface === 'chat')).toBe(true);
		expect(spy).not.toHaveBeenCalled();
	});

	it('keeps the Measurement visit focused when interaction moves into an iframe', () => {
		const hasFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(true);
		window.dispatchEvent(new Event('pointerdown'));
		startPresence('iframe-thread');
		vi.advanceTimersByTime(DWELL_MS);
		expect(JSON.parse(socket.send.mock.calls[0][0] as string).focused).toBe(true);
		socket.send.mockClear();
		const iframe = document.createElement('iframe');
		document.body.append(iframe);
		iframe.focus();
		window.dispatchEvent(new Event('blur'));
		vi.advanceTimersByTime(0);
		const frames = socket.send.mock.calls.map(([raw]) => JSON.parse(raw as string));
		expect(frames).toEqual([]);
		expect(getPresenceTracker()?.claimed).toBe(true);
		hasFocus.mockRestore();
		iframe.remove();
	});

	it('ends Measurement focus when the window is left from inside an iframe', () => {
		const hasFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(true);
		window.dispatchEvent(new Event('pointerdown'));
		startPresence('iframe-leave-thread');
		vi.advanceTimersByTime(DWELL_MS);
		const iframe = document.createElement('iframe');
		document.body.append(iframe);
		iframe.focus();
		socket.send.mockClear();
		// Another app took focus: the iframe is still document.activeElement, hasFocus() is false.
		hasFocus.mockReturnValue(false);
		window.dispatchEvent(new Event('blur'));
		vi.advanceTimersByTime(0);
		const frames = socket.send.mock.calls.map(([raw]) => JSON.parse(raw as string));
		expect(frames.map((frame) => [frame.state, frame.focused])).toEqual([['foreground', false]]);
		expect(document.activeElement).toBe(iframe);
		hasFocus.mockRestore();
		iframe.remove();
	});

	it('on a phone, starts on a visible page and ends when the page is hidden', () => {
		vi.stubGlobal('navigator', { userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)' });
		const hasFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(false);
		const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
		window.dispatchEvent(new Event('pointerdown'));
		startPresence('phone-thread');
		vi.advanceTimersByTime(DWELL_MS);
		const started = socket.send.mock.calls.map(([raw]) => JSON.parse(raw as string));
		expect(started.map((frame) => [frame.state, frame.focused])).toEqual([['foreground', true]]);
		socket.send.mockClear();
		visibility.mockReturnValue('hidden');
		document.dispatchEvent(new Event('visibilitychange'));
		const ended = socket.send.mock.calls.map(([raw]) => JSON.parse(raw as string));
		expect(ended.map((frame) => [frame.state, frame.reason])).toEqual([['background', 'hidden']]);
		hasFocus.mockRestore();
		visibility.mockRestore();
	});

	it('goes idle on the page input clock, the one the Plans pane reads', () => {
		window.dispatchEvent(new Event('pointerdown'));
		startPresence('t9');
		vi.advanceTimersByTime(DWELL_MS);
		socket.send.mockClear();

		// Pointer movement is input only on the page clock; presence must agree.
		vi.advanceTimersByTime(IDLE_CAP_MS - 5_000);
		window.dispatchEvent(new Event('pointermove'));
		vi.advanceTimersByTime(10_000);

		expect(socket.send).not.toHaveBeenCalled();
	});
});

describe('one holder of the document the canvas shows', () => {
	it('holds the displayed thread and its capability, readable as a store', () => {
		updateDocumentOpenCapability('thread-show', { canShow: false, showing: 'artifact-a' });
		expect(get(displayedDocument)).toEqual({ threadId: 'thread-show', value: { canShow: false, showing: 'artifact-a' } });
		updateDocumentOpenCapability('thread-show', { canShow: true, showing: null });
		expect(get(displayedDocument)).toEqual({ threadId: 'thread-show', value: { canShow: true, showing: null } });
	});

	it('clears through the same update function, so nothing claims a stale document', () => {
		updateDocumentOpenCapability('thread-gone', { canShow: false, showing: 'artifact-b' });
		updateDocumentOpenCapability(null, NO_DOCUMENT_OPEN);
		expect(get(displayedDocument)).toBeNull();
	});

	it('shows the viewing store and the published frame the same document for the displayed thread', () => {
		window.dispatchEvent(new Event('pointerdown'));
		startPresence('thread-pub');
		updateDocumentOpenCapability('thread-pub', { canShow: false, showing: 'artifact-c' });
		const published = getPresenceTracker()!.documentOpenCapability;
		expect(published?.showing).toBe('artifact-c');
		expect(get(displayedDocument)?.value.showing).toBe(published?.showing);
		stopPresence();
	});
});
