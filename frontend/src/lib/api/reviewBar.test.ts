import { afterEach, describe, expect, it, vi } from 'vitest';

import { token } from '$lib/stores/auth';
import { answerReview } from './reviewBar';

describe('answerReview', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		token.set(null);
	});

	it('sends the session in the Authorization header, which the Devices listener admits', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({ review: { verdict: 'approved', note: null, feedback_counts: { comments: 0, suggestions: 0, edits: 0 } } }),
		});
		vi.stubGlobal('fetch', fetchMock);
		token.set('session-1');

		await answerReview('art-1', 'approved', '', ['hold-1']);

		const [url, init] = fetchMock.mock.calls[0];
		expect(url).toBe('/api/attention/art-1/clear');
		expect(init.headers).toMatchObject({ Authorization: 'Bearer session-1', 'Content-Type': 'application/json' });
		expect(JSON.parse(init.body)).toMatchObject({ outcome: 'answer', verdict: 'approved', hold_ids: ['hold-1'] });
	});
});
