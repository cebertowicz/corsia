import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from './auth';
import { activeThreadId, draftAccountId } from './threads';
import { loadModels } from './modelsLoad';
import { models } from './modelsState';

beforeEach(() => { vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ models: [] }) }))); });
afterEach(() => { token.set(null); activeThreadId.set(null); vi.unstubAllGlobals(); });

describe('draft account selection', () => {
	it('reads the chosen account through the catalog', async () => {
		token.set('test');
		const fetcher = vi.fn(async (url: string) => ({ ok: true, json: async () => ({ models: [{ id: 'codex/test', runs_on: [{ account_id: url.includes('account_id=chosen') ? 'chosen' : 'default', selected: true }] }] }) }));
		vi.stubGlobal('fetch', fetcher);
		draftAccountId.set('chosen');
		await loadModels();
		expect(fetcher.mock.calls.some(([url]) => url.includes('account_id=chosen'))).toBe(true);
		expect(get(models)[0].runs_on?.[0].account_id).toBe('chosen');
	});
	it('clears the choice when the chat surface is entered or left', () => {
		draftAccountId.set('chosen');
		activeThreadId.set('thread');
		expect(get(draftAccountId)).toBeNull();
		draftAccountId.set('chosen');
		activeThreadId.set(null);
		expect(get(draftAccountId)).toBeNull();
	});
});
