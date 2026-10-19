import { describe, expect, it } from 'vitest';

import { installCause, installFailureCopy } from './setupConnect.svelte';

describe('setup CLI installer failure copy', () => {
	it.each([
		['the download site refused the request', 'site_refused'],
		['the download service temporarily cannot answer', 'connection'],
		['no internet connection', 'connection'],
		['the installer stopped', 'installer_failed'],
	] as const)('maps %s to the safe %s setup cause', (reason, cause) => {
		expect(installCause(reason)).toBe(cause);
	});

	it('keeps a vendor refusal distinct and does not offer retry', () => {
		expect(installFailureCopy('Codex', 'the download site refused the request', 'cli-refused')).toEqual({
			meta: 'Couldn’t download Codex. The download site refused the request. Save a report. Reference: cli-refused',
			retry: false,
		});
	});

	it('uses the approved connection recovery copy when the network is unavailable', () => {
		expect(installFailureCopy('Claude Code', 'no internet connection', 'cli-offline')).toEqual({
			meta: 'Couldn’t download Claude Code. Check your internet connection, then retry.',
			retry: true,
		});
	});

	it('ends generic installer failure copy with its opaque reference and no trailing punctuation', () => {
		expect(installFailureCopy('Claude Code', 'the installer stopped', 'cli-failed')).toEqual({
			meta: 'Couldn’t install Claude Code. Retry, or save a report. Reference: cli-failed',
			retry: true,
		});
	});
});
