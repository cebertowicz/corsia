import { afterEach, beforeEach, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '../stores/threads';
import { handleMessage } from './handlers';
import { deliveryPresentation } from '../messageDelivery';

beforeEach(() => { activeThreadId.set('held-thread'); messages.set([]); });
afterEach(() => { activeThreadId.set(null); messages.set([]); });

it.each([null, 'held-input'])('retains live hold evidence and ignores stale ownership with correlation %s', (inputId) => {
	const base = {
		id: 'held-input', role: 'user', content: 'saved input', created_at: 't0',
		metadata: inputId ? { input_id: inputId } : {}, delivery_state: 'queued',
	};
	function receive(detail: object) {
		handleMessage({ type: 'message_added', thread_id: 'held-thread', message: { ...base, ...detail } }, () => {});
	}
	receive({ delivery_revision: 2, active_turn_id: 'retired-turn', write_outcome: null });
	receive({ delivery_revision: 3, active_turn_id: null, write_outcome: 'proved_no_write', delivery_error: 'checkpoint unavailable' });
	receive({ delivery_revision: 2, active_turn_id: 'retired-turn', write_outcome: null });
	expect(get(messages)).toHaveLength(1);
	expect(get(messages)[0]).toMatchObject({
		delivery_revision: 3, active_turn_id: null, write_outcome: 'proved_no_write',
		delivery_error: 'checkpoint unavailable',
	});
	expect(deliveryPresentation(get(messages)[0], false)?.label).toBe('Waiting for checkpoint');
	receive({ delivery_revision: 4, active_turn_id: null, write_outcome: null, delivery_error: null });
	expect(deliveryPresentation(get(messages)[0], false)?.label).toBe('Sending when ready…');
});
