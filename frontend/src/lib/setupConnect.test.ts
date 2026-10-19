import { describe, expect, it } from 'vitest';

import { installCause, installFailureCopy } from './setupConnect.svelte';

describe('setup CLI installer failure copy', () => {
	it.each([
		['site_refused', 'site_refused'],
		['connection', 'connection'],
		['download_unverified', 'download_unverified'],
		['installer_failed', 'installer_failed'],
	] as const)('keeps API code %s as the safe %s setup cause', (code, cause) => {
		expect(installCause(code)).toBe(cause);
	});

	it('keeps a vendor refusal distinct and does not offer retry', () => {
		expect(installFailureCopy('Codex', 'site_refused', 'cli-refused')).toEqual({
			meta: 'Couldn’t download Codex. The download site refused the request. Save a report. Reference: cli-refused',
			retry: false,
		});
	});

	it('uses the approved connection recovery copy when the network is unavailable', () => {
		expect(installFailureCopy('Claude Code', 'connection', 'cli-offline')).toEqual({
			meta: 'Couldn’t download Claude Code. Check your internet connection, then retry.',
			retry: true,
		});
	});

	it('ends generic installer failure copy with its opaque reference and no trailing punctuation', () => {
		expect(installFailureCopy('Claude Code', 'download_unverified', 'cli-failed')).toEqual({
			meta: 'Couldn’t install Claude Code. Retry, or save a report. Reference: cli-failed',
			retry: true,
		});
	});
});
