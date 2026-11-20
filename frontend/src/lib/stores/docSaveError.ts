import { LiveOperationError } from './documentFailures';

/** One save refusal's body, read the same way for its message and its code. */
function refusalDetail(error: unknown): Record<string, unknown> | string | null {
	const raw = error instanceof Error ? error.message : String(error);
	const body = raw.replace(/^\d+\s+/, '');
	try {
		const detail = (JSON.parse(body) as { detail?: unknown } | null)?.detail;
		if (detail && typeof detail === 'object') return detail as Record<string, unknown>;
		if (typeof detail === 'string') return detail;
	} catch {
		// A non-JSON failure (an HTML error page) carries no structured detail.
	}
	return null;
}

/** What a save failure with no detail says (the network, an HTML error page). A surface with room for more may say its own thing instead of this. */
export const GENERIC_SAVE_FAILURE = "Couldn't save the document — edits are still local";

export function saveErrorMessage(error: unknown): string {
	if (error instanceof LiveOperationError && (error.kind === 'publication_failed' || error.kind === 'setup_failed')) return error.message;
	const detail = refusalDetail(error);
	if (typeof detail === 'string') return detail;
	if (detail) {
		if (typeof detail.line === 'string') return detail.line;
		const message = typeof detail.message === 'string' ? detail.message : "Couldn't save the document";
		const errors = Array.isArray(detail.errors)
			? detail.errors.filter((item: unknown): item is string => typeof item === 'string')
			: [];
		return errors.length ? `${message}: ${errors.join('; ')}` : message;
	}
	return GENERIC_SAVE_FAILURE;
}

/**
 * The stable reason a save was refused (`detail.code`), or null when it names none.
 * A caller that must behave differently for one refusal keys on this, never on wording.
 */
export function saveErrorCode(error: unknown): string | null {
	const detail = refusalDetail(error);
	return detail && typeof detail === 'object' && typeof detail.code === 'string' ? detail.code : null;
}
