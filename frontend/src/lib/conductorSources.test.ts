import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	anyReady,
	cancelLocalDownload,
	deleteLocalModelDownload,
	fetchLocalModelRecommendations,
	fetchSources,
	recheckSources,
	setupLocalSource,
	startLocalModelDownload,
	type ModelSource,
	type SourceAccount,
} from './conductorSources';

function source(id: string, name: string, state: ModelSource['state'], extra: Partial<ModelSource> = {}): ModelSource {
	return { source: id, name, state, reason: null, fix: null, ...extra };
}

function account(state: SourceAccount['state'], extra: Partial<SourceAccount> = {}): SourceAccount {
	return { account_id: 'a1', label: 'me@example.com', label_verified: true, identified: true, state, resets_at: null, corsia_agent_binding: false, ...extra };
}

describe('anyReady', () => {
	it('is true when some account of a vendor is Ready, even beside a limited one', () => {
		const claude = source('claude-code', 'Claude Code', 'ready', { accounts: [account('limited'), account('ready', { account_id: 'a2' })] });
		expect(anyReady([claude])).toBe(true);
	});

	it('is false when every account is limited, signed out, unchecked or has no usable model', () => {
		for (const state of ['limited', 'signed_out', 'unchecked', 'no_usable_model'] as const) {
			expect(anyReady([source('claude-code', 'Claude Code', 'ready', { accounts: [account(state)] })])).toBe(false);
		}
	});

	it('counts a ready local model or endpoint, but not a limited or unchecked one', () => {
		expect(anyReady([source('omlx', 'Local model', 'ready')])).toBe(true);
		expect(anyReady([source('external:x', 'x', 'ready')])).toBe(true);
		expect(anyReady([source('external:x', 'x', 'unverified')])).toBe(false);
		expect(anyReady([source('omlx', 'Local model', 'ready', { limited: true })])).toBe(false);
	});

	it('is false when nothing is signed in', () => {
		expect(anyReady([source('claude-code', 'Claude Code', 'not_installed'), source('codex', 'Codex', 'needs_auth')])).toBe(false);
	});
});

describe('source routes', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('owns source and local-model routes with the caller token', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => ({
			ok: true,
			json: async () => String(input).endsWith('/local/recommendations')
				? { state: 'ready', sections: { best: [] } }
				: String(input).endsWith('/local/setup') ? { ok: true } : { sources: [{ source: 'omlx' }] },
		}));
		vi.stubGlobal('fetch', fetchMock);
		expect(await fetchSources('tok')).toEqual([{ source: 'omlx' }]);
		expect(await recheckSources('tok')).toEqual([{ source: 'omlx' }]);
		expect(await setupLocalSource('tok')).toEqual({ ok: true });
		expect(await fetchLocalModelRecommendations('tok')).toEqual({ state: 'ready', sections: { best: [] } });
		await startLocalModelDownload('tok', 'org/model');
		await cancelLocalDownload('tok');
		await deleteLocalModelDownload('tok', 'org/model');
		expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
			['/api/providers/sources', 'GET'],
			['/api/providers/sources/recheck', 'POST'],
			['/api/providers/local/setup', 'POST'],
			['/api/providers/local/recommendations', 'GET'],
			['/api/providers/local/downloads', 'POST'],
			['/api/providers/local/cancel', 'POST'],
			['/api/providers/local/downloads/org%2Fmodel', 'DELETE'],
		]);
		for (const [, init] of fetchMock.mock.calls.slice(3)) {
			expect(init).toMatchObject({ headers: { Authorization: 'Bearer tok' } });
		}
	});

	it('returns a structured consent requirement so callers can recover', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: false, status: 409, json: async () => ({ detail: { code: 'consent_required' } }),
		})));

		expect(await setupLocalSource('tok')).toEqual({ ok: false, code: 'consent_required' });
	});

	it('throws on a failed read so the panel can say it could not read', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })));
		await expect(fetchSources('tok')).rejects.toThrow('sources 500');
	});
});
