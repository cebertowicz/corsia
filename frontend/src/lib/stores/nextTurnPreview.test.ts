import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchNextTurnPreview } from './nextTurnPreview';

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('next-turn preview request (#1562)', () => {
	it('asks for the selected model and returns the preview row', async () => {
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({ preview: { total_used: 5 } })));
		vi.stubGlobal('fetch', fetchMock);
		expect(await fetchNextTurnPreview('t1', 'tok', 'claude-code/opus')).toEqual({ total_used: 5 });
		const url = String((fetchMock.mock.calls[0] as unknown[])[0]);
		expect(url).toContain('/api/threads/t1/context-snapshot/next-turn?');
		expect(url).toContain('model=claude-code%2Fopus');
	});

	it('returns null when the backend defers to the measured reading', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ preview: null }))));
		await expect(fetchNextTurnPreview('t1', 'tok', null)).resolves.toBeNull();
	});

	it('throws on a failed response so every caller can show it', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response('x', { status: 503 })));
		await expect(fetchNextTurnPreview('t1', 'tok', null)).rejects.toThrow('Context preview failed (503)');
	});
});
