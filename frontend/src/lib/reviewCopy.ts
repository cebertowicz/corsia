/** Shared status and next step for an unfinished review. */
export function reviewContinuationText(reviewer: string, reason?: string | null): string {
	if (reason === 'missing_credentials') {
		return `The review is unfinished: ${reviewer} isn't signed in to its model. Sign in again from the model card, and ${reviewer} will finish it.`;
	}
	if (reason === 'target_unavailable') {
		return `The review is unfinished: ${reviewer}'s model isn't available. Choose another model for ${reviewer}, and it will finish the review.`;
	}
	if (reason === 'binary_resolution') {
		return `The review is unfinished: CORSIA can't find the app ${reviewer} runs on. Ask CORSIA in chat to fix it, and ${reviewer} will finish the review.`;
	}
	return `The review is unfinished. ${reviewer} will finish it.`;
}
