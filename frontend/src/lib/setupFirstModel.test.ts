import { afterEach, describe, expect, it, vi } from 'vitest';

import { selectSetupFirstModel } from './setupFirstModel';

const readyClaude = {
	id: 'claude-code/sonnet',
	name: 'Sonnet',
	size: '',
	backend: 'harness',
	speed: '',
	connection: 'claude-code',
	status: 'ready' as const,
	litellm_model: 'claude-code/sonnet',
};

function response(body: unknown, ok = true) {
	return { ok, status: ok ? 200 : 500, json: async () => body };
}

afterEach(() => vi.unstubAllGlobals());

describe('selectSetupFirstModel', () => {
	it('persists the ready setup source model on CORSIA before chat can open', async () => {
		const calls: Array<{ url: string; init?: RequestInit }> = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			calls.push({ url, init });
			if (url === '/api/corsia-agent?token=setup-token') return response({ target: { thread_id: 'corsia-thread' } });
			if (url === '/api/models?thread_id=corsia-thread') return response({ models: [readyClaude] });
			if (url === '/api/threads/corsia-thread?token=setup-token') {
				expect(init?.method).toBe('PATCH');
				expect(JSON.parse(String(init?.body))).toEqual({ model: 'claude-code/sonnet' });
				return response({ model: 'claude-code/sonnet', model_updated_at: '2026-10-16T00:00:00Z' });
			}
			throw new Error(`unexpected request: ${url}`);
		}));

		await expect(selectSetupFirstModel('setup-token', [
			{ source: 'codex', name: 'Codex', state: 'needs_auth' },
			{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [{ account_id: 'a', label: 'me', label_verified: true, identified: true, state: 'ready', resets_at: null, corsia_agent_binding: true }] },
		])).resolves.toBe(true);
		expect(calls.map(({ url }) => url)).toEqual([
			'/api/corsia-agent?token=setup-token',
			'/api/models?thread_id=corsia-thread',
			'/api/threads/corsia-thread?token=setup-token',
		]);
	});

	it('keeps setup recovery when its ready source has no ready catalog model', async () => {
		const calls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			calls.push(url);
			if (url === '/api/corsia-agent?token=setup-token') return response({ target: { thread_id: 'corsia-thread' } });
			if (url === '/api/models?thread_id=corsia-thread') return response({ models: [{ ...readyClaude, connection: 'codex' }] });
			throw new Error(`unexpected request: ${url}`);
		}));

		await expect(selectSetupFirstModel('setup-token', [
			{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [{ account_id: 'a', label: 'me', label_verified: true, identified: true, state: 'ready', resets_at: null, corsia_agent_binding: true }] },
		])).resolves.toBe(false);
		expect(calls).toEqual([
			'/api/corsia-agent?token=setup-token',
			'/api/models?thread_id=corsia-thread',
		]);
	});
});
