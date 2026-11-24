import { describe, expect, it } from 'vitest';
import { HostAccessApiError } from '$lib/api/hostAccess';
import {
	APPROVAL_CHANGED, APPROVE_FAILED, DENY_FAILED, NEW_DETAILS, NEW_DETAILS_SCRIPT, REJECT_FAILED, REVOKE_FAILED,
	decisionFailure,
} from './decisionFailure';

const SERVER = 'Internal Server Error';
const refused = (status: number, message = SERVER) => new HostAccessApiError(message, status);
const offline = () => new TypeError('Failed to fetch');

describe('decisionFailure', () => {
	it('names what changed on a 409 by decision and kind, never the server line, and reads it again', () => {
		for (const kind of ['lease', 'fill'] as const) {
			for (const decision of ['approve', 'reject'] as const) {
				expect(decisionFailure(refused(409), decision, kind)).toEqual({ notice: NEW_DETAILS, refresh: true });
			}
		}
		for (const decision of ['approve', 'reject'] as const) {
			expect(decisionFailure(refused(409), decision, 'script')).toEqual({ notice: NEW_DETAILS_SCRIPT, refresh: true });
		}
		for (const kind of ['lease', 'fill'] as const) {
			expect(decisionFailure(refused(409), 'revoke', kind)).toEqual({ notice: APPROVAL_CHANGED, refresh: true });
		}
	});

	it('says which press failed on a 500, any other refusal or no connection, with no re-read', () => {
		const cases = [
			['approve', 'lease', APPROVE_FAILED], ['approve', 'script', APPROVE_FAILED], ['approve', 'fill', APPROVE_FAILED],
			['reject', 'lease', REJECT_FAILED], ['reject', 'fill', REJECT_FAILED], ['reject', 'script', DENY_FAILED],
			['revoke', 'lease', REVOKE_FAILED], ['revoke', 'fill', REVOKE_FAILED],
		] as const;
		for (const [decision, kind, line] of cases) {
			for (const caught of [refused(500), refused(503, 'Host access request failed (503)'), refused(403,
				'A current interactive user decision is required'), refused(400), offline()]) {
				const failure = decisionFailure(caught, decision, kind);
				expect(failure).toEqual({ notice: line, refresh: false });
				expect(failure.notice).not.toContain(caught.message);
			}
		}
	});

	it('keeps a gone request on its current path: the server line, then the list without it', () => {
		expect(decisionFailure(refused(404), 'approve', 'lease')).toEqual({ notice: APPROVE_FAILED, refresh: true });
		expect(decisionFailure(refused(404, 'This sign-in request is closed.'), 'approve', 'fill'))
			.toEqual({ notice: 'This sign-in request is closed.', refresh: true });
		expect(decisionFailure(refused(404, 'This sign-in request is closed.'), 'reject', 'fill'))
			.toEqual({ notice: 'This sign-in request is closed.', refresh: true });
		expect(decisionFailure(refused(404, 'This approval has ended.'), 'revoke', 'fill'))
			.toEqual({ notice: 'This approval has ended.', refresh: true });
	});
});
