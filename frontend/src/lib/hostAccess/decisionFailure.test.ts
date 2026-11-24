import { describe, expect, it } from 'vitest';
import { HostAccessApiError } from '$lib/api/hostAccess';
import { APPROVE_FAILED, NEW_DETAILS, decisionFailure } from './decisionFailure';

const refused = (status: number, message = 'Server line.') => new HostAccessApiError(message, status);

describe('decisionFailure', () => {
	it('says a request has new details on any 409, never the server line, and reads it again', () => {
		for (const decision of ['approve', 'reject', 'revoke'] as const) {
			expect(decisionFailure(refused(409), decision, 'lease')).toEqual({ notice: NEW_DETAILS, refresh: true, stale: false });
		}
	});

	it('says Approve failed and keeps it on after a 500 or no connection', () => {
		expect(decisionFailure(refused(500), 'approve', 'lease')).toEqual({ notice: APPROVE_FAILED, refresh: false, stale: false });
		expect(decisionFailure(new TypeError('Failed to fetch'), 'approve', 'script')).toEqual({ notice: APPROVE_FAILED, refresh: false, stale: false });
	});

	it('reads the list again when an approved lease is gone, but a gone fill keeps its server line', () => {
		expect(decisionFailure(refused(404), 'approve', 'lease')).toEqual({ notice: APPROVE_FAILED, refresh: true, stale: false });
		expect(decisionFailure(refused(404, 'This sign-in request is closed.'), 'approve', 'fill'))
			.toEqual({ notice: 'This sign-in request is closed.', refresh: true, stale: false });
	});

	it('keeps the server line for other Reject and Revoke failures and stops trusting the list', () => {
		expect(decisionFailure(refused(503), 'reject', 'lease')).toEqual({ notice: 'Server line.', refresh: false, stale: true });
		expect(decisionFailure(new TypeError('Failed to fetch'), 'revoke', 'fill')).toEqual({ notice: null, refresh: false, stale: true });
	});
});
