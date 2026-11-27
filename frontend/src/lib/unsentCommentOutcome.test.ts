import { describe, expect, it } from 'vitest';
import { RequestRefused } from '$lib/api/docs';
import { CommentConnectionError, PostTimeoutError } from '$lib/api/boundedRequest';
import { unsentOutcome } from './unsentCommentOutcome';

describe('automatic comment retry', () => {
	it.each([408, 429, 502, 503, 504])('retries a transient HTTP %i', (status) => {
		expect(unsentOutcome(new RequestRefused(`${status} failure`, null))).toBe('retry');
	});
	it.each([401, 400, 500, 501, 505])('tells the person about HTTP %i', (status) => {
		expect(unsentOutcome(new RequestRefused(`${status} failure`, null))).toBe('refused');
	});
	it('retries connection loss and timeout, but tells the person about a programming error', () => {
		expect(unsentOutcome(new CommentConnectionError())).toBe('retry');
		expect(unsentOutcome(new PostTimeoutError(100))).toBe('retry');
		expect(unsentOutcome(new TypeError('application bug'))).toBe('refused');
		expect(unsentOutcome(new Error('Cannot send reply: no chat'))).toBe('refused');
	});
});
