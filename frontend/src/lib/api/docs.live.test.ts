import { afterEach, expect, it, vi } from 'vitest';
import { fetchContent } from './docs';

afterEach(() => vi.unstubAllGlobals());

it('requests live admission on the HTTP content load', async () => {
	const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ text: 'text' })));
	vi.stubGlobal('fetch', fetcher);
	await fetchContent('/doc.md', { liveOpen: true });
	expect(new URL(fetcher.mock.calls[0][0], 'http://localhost').searchParams.get('live_open')).toBe('true');
});

it.each(['store_damaged', 'store_unavailable'])('preserves the full typed %s HTTP refusal', async code => {
	const title = 'Live editing is unavailable for this document.';
	const message = 'Verbatim server detail '.repeat(30);
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code, title, message } }), { status: 503 })));
	await expect(fetchContent('/doc.md', { liveOpen: true })).rejects.toMatchObject({ code, title, message });
});

it('curates setup HTTP refusal while preserving its typed retry code and title', async () => {
	const code = 'setup_failed', title = 'Live editing is unavailable for this document.';
	const message = '{"detail":"private fixture setup reason"}';
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code, title, message } }), { status: 503 })));
	await expect(fetchContent('/doc.md', { liveOpen: true })).rejects.toMatchObject({ code, title, message: "Live editing couldn't start for this document." });
});
