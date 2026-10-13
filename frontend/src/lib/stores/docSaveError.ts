import { LiveOperationError } from './documentFailures';

export function saveErrorMessage(error: unknown): string {
	if (error instanceof LiveOperationError && (error.kind === 'publication_failed' || error.kind === 'setup_failed')) return error.message;
	const raw = error instanceof Error ? error.message : String(error);
	const body = raw.replace(/^\d+\s+/, '');
	try {
		const parsed = JSON.parse(body);
		const detail = parsed?.detail;
		if (detail && typeof detail === 'object') {
			if (typeof detail.line === 'string') return detail.line;
			const message = typeof detail.message === 'string' ? detail.message : "Couldn't save the document";
			const errors = Array.isArray(detail.errors)
				? detail.errors.filter((item: unknown): item is string => typeof item === 'string')
				: [];
			return errors.length ? `${message}: ${errors.join('; ')}` : message;
		}
		if (typeof detail === 'string') return detail;
	} catch {
		// Use the local-edits-safe fallback below for non-JSON failures.
	}
	return "Couldn't save the document — edits are still local";
}
