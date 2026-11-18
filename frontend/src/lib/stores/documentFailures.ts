/** Public failure vocabulary and copy; state remains in LiveDocumentSession. */
export const FAILURE_KINDS = ['store_refused', 'publication_failed', 'setup_failed'] as const;
export type FailureKind = typeof FAILURE_KINDS[number];
export type FailureObservation = {
	kind?: string; message?: string; failure_generation?: number; clears_through?: number;
	setup_incarnation?: string; setup_revision?: number;
};
export type BlockingFailure = { kind: FailureKind; message: string; version: number };
export const SLOW_DRAIN_MESSAGE = 'Earlier changes are still being saved on the server. Try again in a moment.';

export function failureKind(kind: string | undefined): FailureKind | null {
	return FAILURE_KINDS.find(value => value === kind) ?? null;
}

export function publicFailureMessage(kind: string | undefined, reason?: string): string {
	if (kind === 'publication_failed') return reason === SLOW_DRAIN_MESSAGE ? reason : "Live updates couldn't be shared for this document.";
	if (kind === 'setup_failed') return "Live editing couldn't start for this document.";
	return reason ?? 'The document operation was refused';
}

export class LiveOperationError extends Error {
	constructor(readonly kind: string | undefined, reason?: string) {
		super(publicFailureMessage(kind, reason));
	}
}
