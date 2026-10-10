import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '$lib/stores/threads';
import { notice, present, THREAD, work } from '$lib/__tests__/fixtures/focusRuns';
import type { TranscriptMessage } from '$lib/components/messageListModel';
import { handleMessage } from './handlers';

beforeEach(() => {
	activeThreadId.set(THREAD);
	messages.set([]);
});

afterEach(() => {
	activeThreadId.set(null);
	messages.set([]);
});

describe('a recovered stamp published for an error the client already holds', () => {
	it('merges onto the loaded row and folds the error into its work block', () => {
		const error = notice('err', 2, '10:01:30');
		messages.set([work('a', 1, '10:01:00'), error, work('b', 3, '10:02:00')]);
		expect(present(get(messages) as TranscriptMessage[]).map((entry) => entry.kind)).toEqual(['fold', 'message', 'fold']);

		handleMessage({
			type: 'message_added', thread_id: THREAD,
			message: {
				id: 'err', role: 'system', content: error.content, created_at: error.created_at, seq: 2,
				metadata: { type: 'cc_error', recovered: true }, delivery_state: 'delivered', delivery_revision: 1,
			},
		}, () => {});

		const rows = get(messages) as TranscriptMessage[];
		expect(rows.filter((row) => row.id === 'err')).toHaveLength(1);
		expect(rows.find((row) => row.id === 'err')?.metadata).toMatchObject({ type: 'cc_error', recovered: true });
		const entries = present(rows);
		expect(entries.map((entry) => entry.kind)).toEqual(['fold']);
	});
});
