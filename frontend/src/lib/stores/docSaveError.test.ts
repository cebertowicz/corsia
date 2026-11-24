import { describe, expect, it } from 'vitest';
import { GENERIC_SAVE_FAILURE, saveErrorIsGeneric, saveErrorMessage } from './docSaveError';
import { LiveOperationError } from './documentFailures';

const body = (detail: unknown, status = 409) => new Error(`${status} ${JSON.stringify({ detail })}`);

describe('whether a save failure has anything specific to say', () => {
	const generic: [string, unknown][] = [
		['a 500 HTML error page', new Error('500 <html><body>Internal Server Error</body></html>')],
		['a network TypeError', new TypeError('Failed to fetch')],
		['a live failure of another kind', new LiveOperationError('connection_lost')],
	];
	const specific: [string, unknown][] = [
		['a JSON detail string', body('This document is deleted.', 404)],
		['a detail with a line', body({ line: 'Choose which text to keep.' })],
		['a detail with a message', body({ code: 'stale_base', message: 'Plain sentence.' })],
		['a publication failure', new LiveOperationError('publication_failed')],
		['a setup failure', new LiveOperationError('setup_failed')],
	];

	it.each(generic)('is generic for %s', (_name, error) => {
		expect(saveErrorIsGeneric(error)).toBe(true);
	});

	it.each(specific)('is not generic for %s', (_name, error) => {
		expect(saveErrorIsGeneric(error)).toBe(false);
	});

	it('agrees with what saveErrorMessage falls back to, for every case above', () => {
		for (const [, error] of generic) expect(saveErrorMessage(error)).toBe(GENERIC_SAVE_FAILURE);
		for (const [, error] of specific) expect(saveErrorMessage(error)).not.toBe(GENERIC_SAVE_FAILURE);
	});
});
