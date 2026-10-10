import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';

import { deliveryPresentation } from '../messageDelivery';
import { WAITING_FOR_MODEL } from '../messageDeliveryCopy';
import { resetLabel } from './modelGroups';
import { activeModelId, models, type Model } from './models';
import { withAccountTurn } from './modelFixtures';
import { limitHeldLine, limitHeldThreadId, PLAN_LIMIT_REASON } from './overageSetting';
import { activeThreadId } from './threads';

const now = new Date('2026-09-30T09:00:00');
const resetsAt = new Date('2026-10-01T15:00:00').toISOString();
const VENDORS = [['codex', 'codex/gpt-5-codex'], ['claude-code', 'claude-code/sonnet']] as const;

function limited(connection: string, id: string, reason: string, resets: string | null): Model {
	return withAccountTurn({
		id, name: id, connection, size: '', backend: 'harness', speed: '',
		status: 'unavailable', status_reason: 'usage_limited', status_resets_at: resets,
		usage: { state: 'limited', usable: false, reason, resets_at: resets, windows: [] },
	});
}

afterEach(() => {
	models.set([]);
	activeModelId.set(null);
	activeThreadId.set(null);
});

describe('limitHeldLine', () => {
	it.each(VENDORS)('reads the same for every reason on %s, with the picker reset format', (connection, id) => {
		const reset = resetLabel(resetsAt, true, now);
		expect(reset).not.toBe('');
		expect(limitHeldLine(limited(connection, id, PLAN_LIMIT_REASON, resetsAt), now))
			.toEqual({ text: `Plan limit reached · resets ${reset}`, planLimit: true });
		expect(limitHeldLine(limited(connection, id, 'five_hour_limit', resetsAt), now))
			.toEqual({ text: `Usage limit reached · resets ${reset}`, planLimit: false });
		expect(limitHeldLine(limited(connection, id, PLAN_LIMIT_REASON, null), now))
			.toEqual({ text: 'Plan limit reached · reset time unknown', planLimit: true });
		expect(limitHeldLine(limited(connection, id, 'rate_limit_event', null), now))
			.toEqual({ text: 'Usage limit reached · reset time unknown', planLimit: false });
	});

	it('is absent while the bound model can run', () => {
		const row = limited('codex', 'codex/gpt-5-codex', PLAN_LIMIT_REASON, resetsAt);
		const [turn] = row.runs_on!;
		const open = { ...row, runs_on: [{ ...turn, status: 'ready' as const, usage: { ...turn.usage, state: 'allowed' as const, usable: true } }] };
		expect(limitHeldLine(open, now)).toBeNull();
		expect(limitHeldLine(undefined, now)).toBeNull();
	});
});

describe('a held person message', () => {
	const queued = { delivery_state: 'queued' as const, thread_id: 'thread-1' };

	it.each(VENDORS)('reads "Waiting for model" while the %s bound row is usage-limited', (connection, id) => {
		models.set([limited(connection, id, PLAN_LIMIT_REASON, resetsAt)]);
		activeModelId.set(id);
		activeThreadId.set('thread-1');
		expect(get(limitHeldThreadId)).toBe('thread-1');
		expect(deliveryPresentation(queued, false, get(limitHeldThreadId) === queued.thread_id))
			.toMatchObject({ ...WAITING_FOR_MODEL, pending: true, failed: false });
	});

	it('reads "Waiting for model" on a message queued behind a turn the limit then stopped', () => {
		const behindTurn = { ...queued, metadata: { delivery_intent: 'queue_after_turn' } };
		expect(deliveryPresentation(behindTurn, false, true)).toMatchObject({ ...WAITING_FOR_MODEL, pending: true, failed: false });
		// A turn still running is what it waits for; so is a ready model.
		expect(deliveryPresentation(behindTurn, true, true)?.label).toBe('Queued');
		expect(deliveryPresentation(behindTurn, false, false)?.label).toBe('Queued');
	});

	it('keeps the sending copy once the row is ready, and never relabels a turn-owned input', () => {
		models.set([withAccountTurn({
			id: 'codex/gpt-5-codex', name: 'codex/gpt-5-codex', connection: 'codex', size: '', backend: 'harness', speed: '',
			status: 'ready', usage: { state: 'allowed', usable: true, windows: [] },
		})]);
		activeModelId.set('codex/gpt-5-codex');
		activeThreadId.set('thread-1');
		expect(get(limitHeldThreadId)).toBeNull();
		expect(deliveryPresentation(queued, false, false)?.label).toBe('Sending when ready…');
		expect(deliveryPresentation({ ...queued, active_turn_id: 'turn-1' }, true, true)).toBeNull();
	});
});
