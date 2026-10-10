import { describe, expect, it } from 'vitest';
import { handleMessage } from './handlers';
import { on } from './events';

describe('doc_activity frames reaching document presence', () => {
	it('delivers the backend frame to subscribers instead of dropping it', () => {
		const frame = {
			type: 'doc_activity',
			artifact_id: 'artifact-1',
			thread_id: 'agent-1',
			agent: 'Documents',
			kind: 'edit',
			span: { from: 2, to: 8 },
			comment_id: null,
			revision: 'r2',
			at: '2026-09-30T05:00:00Z',
		};
		const seen: unknown[] = [];
		const off = on('doc_activity', (message: unknown) => seen.push(message));
		try {
			handleMessage(frame, () => {});
		} finally {
			off();
		}
		expect(seen).toEqual([frame]);
	});

	it('delivers document comment projection changes to an open canvas', () => {
		const frame = { type: 'doc_comment_changed', artifact_id: 'artifact-1' };
		const seen: unknown[] = [];
		const off = on('doc_comment_changed', (message: unknown) => seen.push(message));
		try {
			handleMessage(frame, () => {});
		} finally {
			off();
		}
		expect(seen).toEqual([frame]);
	});
});
