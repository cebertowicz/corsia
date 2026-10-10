import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';

import { handleMessage } from '../ws/handlers';
import { adoptCreatedThread, threads } from '../stores/threads';

function noop() {}

const BASE_THREAD = {
	id: 'fresh-thread',
	user_id: 'u-test',
	profile: 'personal',
	title: 'Fresh Worker',
	model: 'codex/gpt-5-codex',
	project_id: 'corsia-project',
	spawned_by: null,
	created_at: '2026-06-17T00:00:00',
	updated_at: '2026-06-17T00:00:00',
};

describe('session_created visibility frame', () => {
	beforeEach(() => {
		threads.set([]);
	});

	it('adds the created peer thread with model/provider truth preserved', () => {
		handleMessage({ type: 'session_created', session: BASE_THREAD }, noop);

		const stored = get(threads);
		expect(stored).toHaveLength(1);
		expect(stored[0].id).toBe('fresh-thread');
		expect(stored[0].title).toBe('Fresh Worker');
		expect(stored[0].model).toBe('codex/gpt-5-codex');
		expect(stored[0].project_id).toBe('corsia-project');
	});

	it('does not add subagent threads to the sidebar list', () => {
		handleMessage(
			{ type: 'session_created', session: { ...BASE_THREAD, id: 'worker-child', spawned_by: 'parent-1' } },
			noop,
		);

		expect(get(threads)).toHaveLength(0);
	});

	it('adopts a locally-created session without duplicating a live-created row', () => {
		handleMessage({ type: 'session_created', session: BASE_THREAD }, noop);
		adoptCreatedThread({ ...BASE_THREAD, model: 'mock/test-model' });

		const stored = get(threads);
		expect(stored).toHaveLength(1);
		expect(stored[0].id).toBe('fresh-thread');
		expect(stored[0].model).toBe('mock/test-model');
	});
});
