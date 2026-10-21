/** Shared status and next step for an unfinished review. */
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

export function reviewFailureStatus(reason?: string | null): string {
	if (reason === 'missing_credentials') return 'Couldn’t finish: sign-in needed';
	if (reason === 'target_unavailable') return 'Couldn’t finish: choose a model';
	if (reason === 'binary_resolution') return 'Couldn’t finish: ask CORSIA to fix it';
	return 'Couldn’t finish the review';
}
