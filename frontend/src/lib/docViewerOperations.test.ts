import { describe, expect, it, vi } from 'vitest';

const { markArtifactVerified } = vi.hoisted(() => ({
	markArtifactVerified: vi.fn().mockResolvedValue({}),
}));

vi.mock('$lib/api/docs', async (importOriginal) => ({
	RequestRefused: (await importOriginal<typeof import('$lib/api/docs')>()).RequestRefused,
	markArtifactVerified,
	fetchContent: vi.fn(),
}));

import { shouldOfferVerification, type DocumentFreshness } from '$lib/docFreshness';
import { emit } from '$lib/ws/events';
import { createCommentRetryToast, markFreshnessVerified, postDocumentComment, subscribeToDocumentCommentChanges, submitDocumentComment } from './docViewerOperations';
import { toasts } from '$lib/stores/toast';
import { RequestRefused } from '$lib/api/docs';

function freshness(overrides: Partial<DocumentFreshness> = {}): DocumentFreshness {
	return {
		class: 'reference',
		verified_at: '2026-09-12T12:00:00Z',
		age_days: 91,
		due_at: '2026-10-03T12:00:00Z',
		state: 'overdue',
		owner_name: 'Structured Plans',
		...overrides,
	};
}

describe('comment submission failure delivery', () => {
	it('reports the failure and preserves its rejection for the draft-owning composer', async () => {
		const error = new Error('save refused');
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
		const rememberFailureToast = vi.fn();
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			await expect(submitDocumentComment(async () => { throw error; }, rememberFailureToast)).rejects.toBe(error);
			expect(report).toHaveBeenCalledWith("Couldn't post the comment — try again", 'error');
			expect(rememberFailureToast).toHaveBeenCalledWith('toast');
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('shows the canonical failure toast when a caller has no viewer callback', async () => {
		const error = new Error('reply refused');
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'reply-toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			await expect(submitDocumentComment(async () => { throw error; })).rejects.toBe(error);
			expect(report).toHaveBeenCalledWith("Couldn't post the comment — try again", 'error');
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it.each([
		['no_viewing_thread', 'Open this document from a chat to tag agents. Your comment is still here.'],
		['tag_unavailable', 'An agent you tagged can’t be tagged from this chat. Remove its tag and post again. Your comment is still here.'],
	])('names a %s tag refusal by its kind, ahead of the surface message', async (kind, copy) => {
		const error = new RequestRefused('400 {}', kind);
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			await expect(submitDocumentComment(async () => { throw error; }, undefined, () => 'surface')).rejects.toBe(error);
			expect(report).toHaveBeenCalledWith(copy, 'error');
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('rejects a missing artifact before attempting to post a comment', async () => {
		const ensureArtifactId = vi.fn().mockResolvedValue(null);
		const addComment = vi.fn();

		await expect(postDocumentComment(ensureArtifactId, addComment)).rejects.toThrow();

		expect(addComment).not.toHaveBeenCalled();
	});

	it('dismisses only the viewer instance’s remembered failure after a successful retry', () => {
		const dismiss = vi.fn();
		const firstViewer = createCommentRetryToast({ dismiss });
		const secondViewer = createCommentRetryToast({ dismiss });
		firstViewer.remember('first-failure');
		secondViewer.remember('second-failure');

		firstViewer.dismissAfterSuccess();

		expect(dismiss).toHaveBeenCalledTimes(1);
		expect(dismiss).toHaveBeenCalledWith('first-failure');
	});
});

describe('markFreshnessVerified', () => {
	it('replaces the canonical freshness state and clears a previously due document', async () => {
		const verified = await markFreshnessVerified('artifact-1', freshness());

		expect(markArtifactVerified).toHaveBeenCalledWith('artifact-1');
		expect(verified).toMatchObject({
			state: 'current',
			age_days: 0,
			due_at: null,
		});
		expect(shouldOfferVerification(verified)).toBe(false);
	});
});

describe('subscribeToDocumentCommentChanges', () => {
	it('reloads only the currently mounted artifact and unsubscribes on teardown', () => {
		const reload = vi.fn();
		const stop = subscribeToDocumentCommentChanges(() => 'artifact-current', reload);
		emit('doc_comment_changed', { artifact_id: 'artifact-other' });
		emit('doc_comment_changed', { artifact_id: 'artifact-current' });
		stop();
		emit('doc_comment_changed', { artifact_id: 'artifact-current' });
		expect(reload).toHaveBeenCalledTimes(1);
		expect(reload).toHaveBeenCalledWith('artifact-current');
	});
});
