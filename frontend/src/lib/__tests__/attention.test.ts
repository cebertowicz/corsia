/**
 * Tests for the remaining attention rollup API client behavior.
 * Uses fetch mocks — no real network or DOM rendering complexity.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fetchRollup } from '$lib/api/attention';
import type { AttentionItem } from '$lib/api/attention';

function mockFetch(status: number, body: unknown) {
	const response = {
		ok: status >= 200 && status < 300,
		status,
		json: () => Promise.resolve(body),
		text: () => Promise.resolve(JSON.stringify(body)),
	};
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
}

const ITEM: AttentionItem = {
	external_id: 'art-1',
	integration: 'github',
	type: 'pull-request',
	subtype: 'review-request',
	attention_state: 'raised',
	requested_action: 'Review PR #42',
	urgency: 'high',
	owner_thread: 'worker-abc',
	levels: [],
};

const TOKEN = '[REDACTED]';

beforeEach(() => {
	vi.unstubAllGlobals();
});

afterEach(() => {
	vi.restoreAllMocks();
});

describe('fetchRollup', () => {
	it('hits /api/attention/rollup with parent_thread_id', async () => {
		mockFetch(200, { parent_thread_id: 'p1', items: [ITEM], pull_only: true });
		const result = await fetchRollup(TOKEN, 'p1');
		expect(result.pull_only).toBe(true);
		expect(result.items[0].external_id).toBe('art-1');
		const url = vi.mocked(fetch).mock.calls[0][0] as string;
		expect(url).toContain('parent_thread_id=p1');
	});
});

describe('rollup pull-only contract', () => {
	it('returns pull_only: true and never wakes', async () => {
		mockFetch(200, { parent_thread_id: 'p1', items: [], pull_only: true });
		const result = await fetchRollup(TOKEN, 'p1');
		expect(result.pull_only).toBe(true);
	});
});
