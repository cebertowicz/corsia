import { describe, expect, it } from 'vitest';
import { handleMessage } from './handlers';
import { on } from './events';

describe('live document wire dispatch', () => {
	it.each(['doc_opened', 'doc_update', 'doc_ack', 'doc_received', 'doc_saved', 'doc_error', 'doc_cleared', 'doc_positions'])('delivers %s to its document owner unchanged', type => {
		const frame = { type, artifact_id: 'artifact', store_id: 'store', message_ids: ['message'] };
		const seen: unknown[] = [];
		const stop = on(type, value => seen.push(value));
		try { handleMessage(frame, () => {}); } finally { stop(); }
		expect(seen).toEqual([frame]);
	});
});
