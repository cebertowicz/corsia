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
				expect(decisionFailure(refused(409), decision, kind)).toEqual({ notice: NEW_DETAILS, refresh: true, gone: false });
			}
		}
		for (const decision of ['approve', 'reject'] as const) {
			expect(decisionFailure(refused(409), decision, 'script'))
				.toEqual({ notice: NEW_DETAILS_SCRIPT, refresh: true, gone: false });
		}
		for (const kind of ['lease', 'fill'] as const) {
			expect(decisionFailure(refused(409), 'revoke', kind)).toEqual({ notice: APPROVAL_CHANGED, refresh: true, gone: false });
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
				expect(failure).toEqual({ notice: line, refresh: false, gone: false });
				expect(failure.notice).not.toContain(caught.message);
			}
		}
	});

	// A gone request or approval (404), for each button by kind: the server's own detail, and a 404 with no body.
	const GONE_DETAIL = {
		lease: 'Host-access lease not found',
		script: 'Script request not found',
		fill: 'This sign-in request is closed. Ask the agent to try again if it still needs to sign in.',
	} as const;
	const NO_BODY = 'Host access request failed (404)';
	const goneCases = [
		['reject', 'lease', 'This request is closed, so there’s nothing to reject.'],
		['reject', 'fill', 'This request is closed, so there’s nothing to reject.'],
		['reject', 'script', 'This request is closed, so there’s nothing to deny.'],
		['revoke', 'lease', 'This approval has ended, so there’s nothing to revoke.'],
		['revoke', 'fill', 'This approval has ended, so there’s nothing to revoke.'],
		['revoke', 'script', 'This approval has ended, so there’s nothing to revoke.'],
		['approve', 'lease', 'Couldn’t approve the request. Try again.'],
		['approve', 'script', 'Couldn’t approve the request. Try again.'],
	] as const;
	it.each(goneCases)('gives %s on a gone %s request one line of its own, never server text', (
		decision, kind, line,
	) => {
		for (const serverText of [GONE_DETAIL[kind], NO_BODY]) {
			const failure = decisionFailure(refused(404, serverText), decision, kind);
			expect(failure).toEqual({ notice: line, refresh: true, gone: true });
			expect(failure.notice).not.toContain(serverText);
		}
	});

	it('keeps a saved login’s own closed-request line on Approve, the one 404 that passes server text through', () => {
		expect(decisionFailure(refused(404, GONE_DETAIL.fill), 'approve', 'fill'))
			.toEqual({ notice: GONE_DETAIL.fill, refresh: true, gone: true });
	});
});
