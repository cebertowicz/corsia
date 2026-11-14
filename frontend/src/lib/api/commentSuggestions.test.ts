import { afterEach, describe, expect, it, vi } from 'vitest';
import { decideSuggestion, SuggestionRefused } from './commentSuggestions';

afterEach(() => vi.unstubAllGlobals());

describe('decideSuggestion', () => {
	it('preserves a conflict detail for inline display', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: false, status: 409,
			json: vi.fn().mockResolvedValue({ detail: 'The text changed on the server', kind: 'stale' }),
		}));
		const refusal = decideSuggestion('comment-1', 'accept');
		await expect(refusal).rejects.toThrow('The text changed on the server');
		await expect(refusal).rejects.toBeInstanceOf(SuggestionRefused);
		await expect(refusal).rejects.toMatchObject({ status: 409, kind: 'stale' });
	});

	it('leaves the kind empty when the server sends none, and shows the fallback sentence, not its text', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 400, json: vi.fn().mockResolvedValue({ detail: 'Bad' }) }));
		const refusal = decideSuggestion('comment-1', 'accept');
		await expect(refusal).rejects.toMatchObject({ status: 400, kind: null });
		await expect(refusal).rejects.toThrow('Couldn’t accept this suggestion. Try again.');
	});

	it('shows the fallback sentence for a crash page with no JSON, for either decision', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 502, json: vi.fn().mockRejectedValue(new SyntaxError('Unexpected token <')) }));
		await expect(decideSuggestion('comment-1', 'reject')).rejects.toThrow('Couldn’t reject this suggestion. Try again.');
	});

	it('lets a request that got no answer fail as itself, not as a refusal', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
		const failure = decideSuggestion('comment-1', 'accept');
		await expect(failure).rejects.toBeInstanceOf(TypeError);
		await expect(failure).rejects.not.toBeInstanceOf(SuggestionRefused);
	});

	it('sends the all-from-author batch flag', async () => {
		const result = { accepted: ['comment-1'], skipped: [], text: 'updated', revision: 'r2' };
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: vi.fn().mockResolvedValue(result),
		});
		vi.stubGlobal('fetch', fetchMock);

		await expect(decideSuggestion('comment-1', 'accept', true)).resolves.toEqual(result);
		const [, request] = fetchMock.mock.calls[0];
		expect(JSON.parse(request.body)).toEqual({ decision: 'accept', all_from_author: true });
	});
});
