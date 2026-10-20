import { afterEach, describe, expect, it, vi } from 'vitest';

import { finishSetupWithBestEffortSelection, selectSetupFirstModel } from './setupFirstModel';

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
				expect(JSON.parse(String(init?.body))).toEqual({ model: 'claude-code/sonnet', source: 'setup' });
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

	it('prefers a ready Claude Code or Codex over the local model, whatever order they became ready in', async () => {
		const saved: unknown[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/corsia-agent')) return response({ target: { thread_id: 'corsia-thread' } });
			if (url === '/api/models?thread_id=corsia-thread') return response({ models: [{ ...readyClaude, id: 'omlx/qwen', connection: 'omlx', litellm_model: 'omlx/qwen' }, readyClaude] });
			if (url.startsWith('/api/threads/corsia-thread')) { saved.push(JSON.parse(String(init?.body))); return response({ model: 'claude-code/sonnet', model_updated_at: '2026-10-16T00:00:00Z' }); }
			throw new Error(`unexpected request: ${url}`);
		}));

		await expect(selectSetupFirstModel('setup-token', [
			{ source: 'omlx', name: 'This computer', state: 'ready' },
			{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [{ account_id: 'a', label: 'me', label_verified: true, identified: true, state: 'ready', resets_at: null, corsia_agent_binding: true }] },
		])).resolves.toBe(true);
		expect(saved).toEqual([{ model: 'claude-code/sonnet', source: 'setup' }]);
	});

	it('chooses the local model only when no cloud source is ready', async () => {
		const saved: unknown[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/corsia-agent')) return response({ target: { thread_id: 'corsia-thread' } });
			if (url === '/api/models?thread_id=corsia-thread') return response({ models: [{ ...readyClaude, id: 'omlx/qwen', connection: 'omlx', litellm_model: 'omlx/qwen' }] });
			if (url.startsWith('/api/threads/corsia-thread')) { saved.push(JSON.parse(String(init?.body))); return response({ model: 'omlx/qwen', model_updated_at: '2026-10-16T00:00:00Z' }); }
			throw new Error(`unexpected request: ${url}`);
		}));

		await expect(selectSetupFirstModel('setup-token', [{ source: 'omlx', name: 'This computer', state: 'ready' }])).resolves.toBe(true);
		expect(saved).toEqual([{ model: 'omlx/qwen', source: 'setup' }]);
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

	it('finishes setup when the ready source has no matching catalog model', async () => {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/corsia-agent?token=[REDACTED]') return response({ target: { thread_id: 'corsia-thread' } });
			if (url === '/api/models?thread_id=corsia-thread') return response({ models: [{ ...readyClaude, connection: 'codex' }] });
			throw new Error(`unexpected request: ${url}`);
		}));
		const onFinished = vi.fn();

		await finishSetupWithBestEffortSelection('setup-token', [
			{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [{ account_id: 'a', label: 'me', label_verified: true, identified: true, state: 'ready', resets_at: null, corsia_agent_binding: true }] },
		], onFinished);

		expect(onFinished).toHaveBeenCalledOnce();
	});
});
