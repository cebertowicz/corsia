import type { DocumentReview, DocumentReviewLifecycleState } from '$lib/api/docReviews';

/** User-facing review status and recovery copy comes from the stored request state. */
export function reviewContinuationText(reviewer: string, reason?: string | null): string {
	if (reason === 'missing_credentials') {
		return `Couldn’t finish ${reviewer}’s review. Sign in again in Settings › Providers.`;
	}
	if (reason === 'target_unavailable') {
		return `Couldn’t finish ${reviewer}’s review. Choose another model for ${reviewer}.`;
	}
	if (reason === 'binary_resolution') {
		return `Couldn’t finish ${reviewer}’s review. Ask CORSIA in chat to fix it.`;
	}
	return `The review is unfinished. ${reviewer} will finish it.`;
}

export function reviewStatus(outcome: DocumentReview['outcome'], state: DocumentReviewLifecycleState, reviewer: string, createdAt: string, reason?: string | null): string {
	if (outcome === 'ended_without_comments') return 'Ended without comments';
	if (state === 'forked') return `Requested ${new Date(createdAt).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
	if (state === 'handed_back') return `Unfinished. ${reviewer} will finish it.`;
	if (reason === 'missing_credentials') return 'Couldn’t finish because sign-in is needed.';
	if (reason === 'target_unavailable') return 'Couldn’t finish. Choose another model.';
	if (reason === 'binary_resolution') return 'Couldn’t finish. Ask CORSIA to fix it.';
	return `Unfinished. ${reviewer} will finish it.`;
}
