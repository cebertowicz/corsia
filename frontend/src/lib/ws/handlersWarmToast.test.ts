import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { activeThreadId } from '../stores/threads';
import { warmToast, dismissWarmToast, showWarmToast } from '../stores/warmToast';
import { handleMessage } from './handlers';
import { _resetTurnStateForTests } from './turnState';

const THREAD_ID = 'warm-toast-session';
const TURN = { turn_id: 'turn-1', turn_order: 1 };

beforeEach(() => {
	dismissWarmToast();
	_resetTurnStateForTests();
	activeThreadId.set(THREAD_ID);
});

afterEach(() => {
	dismissWarmToast();
	_resetTurnStateForTests();
	activeThreadId.set(null);
	vi.restoreAllMocks();
});

describe('warm-state websocket events', () => {
	it('surfaces interrupted cache warming as a visible cold-prefill cue', async () => {
		handleMessage({
			type: 'cache_warm_interrupted',
			thread_id: 'sess-1',
			message: 'Cache still warming; first response may take longer.',
		}, vi.fn());

		await vi.waitFor(() => {
			expect(get(warmToast)).toMatchObject({
				visible: true,
				message: 'Cache still warming; first response may take longer.',
				status: 'rebuilding',
			});
		});
	});
});

describe('a rebuild spinner never outlives the turn it describes', () => {
	it('settles when the turn speaks its first token, reasoning or not', () => {
		showWarmToast('Rebuilding cache…', 'rebuilding');
		handleMessage({
			type: 'text_delta', thread_id: THREAD_ID, content: 'hi', ...TURN,
		}, vi.fn());
		expect(get(warmToast).visible).toBe(false);
	});

	it('settles when the turn ends without ever producing a token', () => {
		showWarmToast('Rebuilding cache…', 'rebuilding');
		handleMessage({
			type: 'error', thread_id: THREAD_ID, message: 'boom', ...TURN,
		}, vi.fn());
		expect(get(warmToast).visible).toBe(false);
	});

	it('settles when the turn is cancelled before it answers', () => {
		showWarmToast('Rebuilding cache…', 'rebuilding');
		handleMessage({
			type: 'turn_cancelled', thread_id: THREAD_ID, ...TURN,
		}, vi.fn());
		expect(get(warmToast).visible).toBe(false);
	});

	it('leaves the warm-call toast alone — that one reports its own completion', () => {
		showWarmToast('Warming conversation…', 'warming');
		handleMessage({
			type: 'text_delta', thread_id: THREAD_ID, content: 'hi', ...TURN,
		}, vi.fn());
		expect(get(warmToast)).toMatchObject({ visible: true, status: 'warming' });
	});
});
