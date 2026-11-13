import { describe, expect, it } from 'vitest';
import { ok, patchFailureOf, RequestRefused } from './docs';

const answer = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('why a flag change was not taken', () => {
	it('reads the server reference and status from a server-side failure', async () => {
		const refusal = await ok(answer(500, { detail: 'Something went wrong in CORSIA. Try again.', reference: 'K3M7QD' })).catch((e) => e);
		expect(refusal).toBeInstanceOf(RequestRefused);
		expect(patchFailureOf(refusal)).toEqual({ cause: 'server', reference: 'K3M7QD' });
	});

	it('a refusal below 500 names no reference, since the server mints none for it', async () => {
		const refusal = await ok(answer(404, { detail: 'Comment not found' })).catch((e) => e);
		expect(patchFailureOf(refusal)).toEqual({ cause: 'other', reference: null });
	});

	it('a request that got no answer is the connection', () => {
		expect(patchFailureOf(new TypeError('Failed to fetch'))).toEqual({ cause: 'connection', reference: null });
		expect(patchFailureOf(new Error('anything else'))).toEqual({ cause: 'other', reference: null });
	});
});
