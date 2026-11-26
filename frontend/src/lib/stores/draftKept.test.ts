import { describe, expect, it } from 'vitest';
import { DRAFT_KEPT, failedLine, withoutDraftKept } from './draftKept';
import { NOT_KEPT_MESSAGE } from './keptCommentDrafts.svelte';
import { CAPTURE_FAILURE, SELECTION_LOST } from './commentSelection';
import { EARLIER_VERSION_POSTED } from './docComments';
import { POST_FAILED, TAG_REFUSALS } from '$lib/docViewerOperations';
import { liveFailureMessage } from '$lib/docViewerCommentDraft.svelte';
import { DIAGRAM_CHANGED_MESSAGE } from '$lib/commentFailureMessage';

const timeout = Object.assign(new Error('request timed out after 60000ms (may have committed server-side)'), { name: 'PostTimeoutError' });
/** Every reason a document's or an image's comment box, or a reply's field, can show under it, as the source words it. */
const REASONS: [string, string][] = [
	['POST_FAILED', POST_FAILED],
	...Object.entries(CAPTURE_FAILURE).map(([name, reason]): [string, string] => [`CAPTURE_FAILURE.${name}`, reason]),
	...Object.entries(CAPTURE_FAILURE).map(([name, reason]): [string, string] => [`live CAPTURE_FAILURE.${name}`, liveFailureMessage(new Error(reason))]),
	['DIAGRAM_CHANGED_MESSAGE', DIAGRAM_CHANGED_MESSAGE],
	['live, not an Error', liveFailureMessage('lost')],
	['live, selection lost', liveFailureMessage(new Error(SELECTION_LOST))],
	['live, 409', liveFailureMessage(new Error('409 {"detail":"changed"}'))],
	['live, timeout', liveFailureMessage(timeout)],
	['live, lost connection', liveFailureMessage(new TypeError('Failed to fetch'))],
	['live, server error', liveFailureMessage(new Error('500 synthetic'))],
	['live, refusal with a reason', liveFailureMessage(new Error('404 {"detail":"This document is no longer shared."}'))],
	['live, refusal without one', liveFailureMessage(new Error('422 {"detail":[{"msg":"bad"}]}'))],
];
/**
 * Reasons that say the text is still on screen, not that it is kept: still true when this device does not keep the draft,
 * so they show whole beside the not-kept sentence. (The server's not-commentable sentence, `NOT_COMMENTABLE` in
 * backend/api/doc_comment_access.py, is the third: it is shown as the server gives it and has no frontend constant.)
 */
const STILL_ON_SCREEN: [string, string][] = [
	['EARLIER_VERSION_POSTED', EARLIER_VERSION_POSTED],
	...Object.entries(TAG_REFUSALS).map(([kind, reason]): [string, string] => [`TAG_REFUSALS.${kind}`, reason]),
];
const SAYS_KEPT = /\bkept\b|still here|carry over/i;

describe('the line under a box whose draft this device does not keep', () => {
	it('takes the kept sentence out from the middle of a reason, with one space', () => {
		expect(withoutDraftKept(POST_FAILED)).toBe('Couldn’t post the comment. Try again.');
	});
	it('takes the kept sentence out from the end of a reason, with one space', () => {
		expect(withoutDraftKept(liveFailureMessage(new Error('404 {"detail":"This document is no longer shared."}')))).toBe('Couldn’t post the comment: This document is no longer shared.');
		expect(withoutDraftKept(DIAGRAM_CHANGED_MESSAGE)).toBe('Place the pin on the current diagram.');
	});
	it('takes it out from the middle of the timeout reason, which keeps its next step', () => {
		expect(withoutDraftKept(liveFailureMessage(timeout))).toBe('Couldn’t confirm the comment was posted. Check the comments before you post it again.');
	});
	it('names a next step in every reason without one of the server’s: a failure with no reason is the plain post failure', () => {
		for (const failure of ['lost', new Error('400 <html>Bad request</html>'), new Error('422 {"detail":[{"msg":"bad"}]}')]) expect(liveFailureMessage(failure)).toBe(POST_FAILED);
		for (const [, reason] of REASONS) expect(reason).not.toMatch(/\bwe\b/i);
	});
	it('says a full share in one line, the reason first', () => {
		expect(failedLine(POST_FAILED, NOT_KEPT_MESSAGE.full)).toBe('Couldn’t post the comment. Try again. This device has no room left to keep this draft. Post it or copy it before you close CORSIA.');
		expect(failedLine(POST_FAILED, NOT_KEPT_MESSAGE['too-long'])).toBe('Couldn’t post the comment. Try again. Too long to keep on this device. Post it or copy it before you close CORSIA.');
	});
	it('leaves the reason whole while the device keeps the draft, and says only why it is not kept when nothing failed', () => {
		expect(failedLine(POST_FAILED, '')).toBe(POST_FAILED);
		expect(failedLine('', NOT_KEPT_MESSAGE.refused)).toBe(NOT_KEPT_MESSAGE.refused);
	});
	describe.each(Object.entries(NOT_KEPT_MESSAGE))('%s', (_why, notKept) => {
		it.each(REASONS)('%s never says the draft is kept, and still says why', (_name, reason) => {
			const line = failedLine(reason, notKept);
			expect(reason).toContain(DRAFT_KEPT);
			expect(line).not.toMatch(SAYS_KEPT);
			expect(line).toBe(`${withoutDraftKept(reason)} ${notKept}`);
			expect(withoutDraftKept(reason).length).toBe(reason.length - DRAFT_KEPT.length - 1);
			expect(line).not.toMatch(/ {2}| \./);
		});
		it.each(STILL_ON_SCREEN)('%s shows whole: it says the text is on screen', (_name, reason) => {
			expect(reason).not.toContain(DRAFT_KEPT);
			expect(failedLine(reason, notKept)).toBe(`${reason} ${notKept}`);
		});
	});
});
