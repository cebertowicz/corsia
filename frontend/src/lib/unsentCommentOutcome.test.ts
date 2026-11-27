import { describe, expect, it } from 'vitest';
import { RequestRefused } from '$lib/api/docs';
import { CommentConnectionError, PostTimeoutError } from '$lib/api/boundedRequest';
import { unsentOutcome, unsentReason } from './unsentCommentOutcome';
import { SELECTION_LOST } from '$lib/stores/commentSelection';
import { CaptureFailed, type CaptureCode } from '$lib/stores/captureFailed';

describe('automatic comment retry', () => {
	it.each([401, 408, 429, 502, 503, 504])('retries a transient HTTP %i', (status) => {
		expect(unsentOutcome(new RequestRefused(`${status} failure`, null))).toBe('retry');
	});
	it.each([400, 500, 501, 505])('tells the person about HTTP %i', (status) => {
		expect(unsentOutcome(new RequestRefused(`${status} failure`, null))).toBe('refused');
	});
	it.each<CaptureCode>(['changed', 'offline', 'unanswered', 'settling', 'store_mismatch', 'identity'])('retries live capture %s', (code) => {
		expect(unsentOutcome(new CaptureFailed(code, 'capture failed'))).toBe('retry');
	});
	it.each<CaptureCode>(['lost', 'refused', 'hash'])('tells the person about live capture %s', (code) => {
		expect(unsentOutcome(new CaptureFailed(code, 'capture failed'))).toBe('refused');
	});
	it('explains a lost selection and a server refusal without exposing a local hash error', () => {
		expect(unsentReason(new CaptureFailed('lost', SELECTION_LOST))).toContain('selected text changed or was removed');
		expect(unsentReason(new CaptureFailed('refused', 'The server refused this selection.'))).toBe('The server refused this selection.');
		expect(unsentReason(new CaptureFailed('hash', 'private local exception'))).not.toContain('private local exception');
	});
	it('retries connection loss and timeout, but tells the person about a programming error', () => {
		expect(unsentOutcome(new CommentConnectionError())).toBe('retry');
		expect(unsentOutcome(new PostTimeoutError(100))).toBe('retry');
		expect(unsentOutcome(new TypeError('application bug'))).toBe('refused');
		expect(unsentOutcome(new Error('Cannot send reply: no chat'))).toBe('refused');
	});
});
