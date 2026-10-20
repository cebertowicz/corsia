import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import { isNavAgent, threads, type Thread } from '$lib/stores/threads';
import { handleMessage } from './handlers';

const resetHeartbeat = () => {};

function row(): Thread {
	return {
		id: 'created',
		user_id: 'user',
		profile: 'personal',
		title: 'Docs owner',
		model: 'native/test',
		spawned_by: null,
		agent_enabled: 0,
		agent_responsibility: null,
		agent_status: 'active',
		created_at: '2026-10-16T10:00:00',
		updated_at: '2026-10-16T10:00:00',
	};
}

describe('session_updated roster change', () => {
	beforeEach(() => threads.set([row()]));

	it('moves a cached conversation into Agents on promote and back on demote', () => {
		expect(isNavAgent(get(threads)[0])).toBe(false);

		handleMessage({
			type: 'session_updated',
			thread_id: 'created',
			agent_enabled: 1,
			agent_responsibility: 'Owns docs.',
			agent_paths: '[]',
			agent_domains: '[]',
			agent_status: 'active',
			agent_notes: null,
			updated_at: '2026-10-16T10:05:00.000000',
		}, resetHeartbeat);
		expect(isNavAgent(get(threads)[0])).toBe(true);

		handleMessage({
			type: 'session_updated',
			thread_id: 'created',
			agent_enabled: 0,
			agent_responsibility: 'Owns docs.',
			agent_status: 'active',
			updated_at: '2026-10-16T10:06:00.000000',
		}, resetHeartbeat);
		const demoted = get(threads)[0];
		expect(isNavAgent(demoted)).toBe(false);
		expect(demoted.agent_responsibility).toBe('Owns docs.');
	});

	it('leaves roster fields alone on a frame that carries none', () => {
		threads.set([{ ...row(), agent_enabled: 1, agent_responsibility: 'Owns docs.' }]);
		handleMessage({ type: 'session_updated', thread_id: 'created', title: 'Renamed' }, resetHeartbeat);
		expect(isNavAgent(get(threads)[0])).toBe(true);
	});
});
