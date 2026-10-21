/** Shared status for an unfinished review with a live continuation task. */
export function reviewContinuationText(reviewer: string): string {
	return `The review is unfinished. ${reviewer} will finish it.`;
}
