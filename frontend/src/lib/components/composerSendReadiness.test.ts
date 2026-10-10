import { describe, expect, it } from 'vitest';
import type { Model } from '$lib/stores/models';
import { composerCanSend, hasReadySwitchDestination } from './composerSendReadiness';

const model = (id: string, status: Model['status']): Model => ({
	id, name: id, size: '', backend: 'local', speed: '', status,
});
const usageLimitedDestination = (runnable: 'ready' | 'unavailable'): Model => ({
	...model('destination', 'unavailable'),
	connection: 'codex',
	runnable: { status: runnable, status_reason: runnable === 'ready' ? null : 'usage_limited' },
	runs_on: [
		{
			account_id: 'limited', selected: true, status: 'unavailable', status_reason: 'usage_limited',
			resets_at: '2099-01-01T00:00:00Z', usage: { state: 'limited', usable: false, windows: [] },
		},
		{
			account_id: 'ready-sibling', selected: false, status: 'ready', status_reason: null,
			resets_at: null, usage: { state: 'allowed', usable: true, windows: [] },
		},
	],
});
const models = [model('ready', 'ready'), model('down', 'unavailable')];

describe('composerCanSend', () => {
	it('sends on a ready model and holds on an unavailable one', () => {
		const thread = { model: 'down', pending_model: null, switch_status: null };
		expect(composerCanSend(thread, models[0], models)).toBe(true);
		expect(composerCanSend(thread, models[1], models)).toBe(false);
	});

	it('sends from a thread with no model yet, for the server to hold', () => {
		const thread = { model: null, pending_model: null, switch_status: null };
		expect(composerCanSend(thread, undefined, models)).toBe(true);
		expect(composerCanSend(undefined, undefined, models)).toBe(false);
	});

	it('sends into a pending switch only when its destination can run', () => {
		const ready = { model: 'down', pending_model: 'ready', switch_status: 'preparing' };
		const failed = { ...ready, switch_status: 'failed' };
		expect(hasReadySwitchDestination(ready, models)).toBe(true);
		expect(composerCanSend(ready, models[1], models)).toBe(true);
		expect(composerCanSend(failed, models[1], models)).toBe(false);
	});

	it('uses the server runnable state for a usage-limited destination with a ready sibling', () => {
		const thread = { model: 'down', pending_model: 'destination', switch_status: 'preparing' };
		const ready = usageLimitedDestination('ready');
		const unavailable = usageLimitedDestination('unavailable');

		expect(hasReadySwitchDestination(thread, [ready])).toBe(true);
		expect(hasReadySwitchDestination(thread, [unavailable])).toBe(false);
	});
});
