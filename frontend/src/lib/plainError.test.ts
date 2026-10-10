import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	SERVER_UNREACHABLE,
	isMachineCode,
	plainErrorText,
	plainResponseError,
} from './plainError';

describe('plainErrorText', () => {
	it('replaces a machine code with the caller’s sentence', () => {
		expect(plainErrorText('provider_write_outcome_unknown', 'Could not send it'))
			.toBe('Could not send it');
		expect(plainErrorText('not_authorized_for_session', 'No access'))
			.toBe('No access');
	});

	it('passes through copy a human wrote for a human', () => {
		expect(plainErrorText('The selected model is no longer available', 'fallback'))
			.toBe('The selected model is no longer available');
	});

	it('falls back for anything that is not a non-empty string', () => {
		expect(plainErrorText({ code: 'x' }, 'fallback')).toBe('fallback');
		expect(plainErrorText(undefined, 'fallback')).toBe('fallback');
		expect(plainErrorText('   ', 'fallback')).toBe('fallback');
	});

	it('recognises codes without mistaking ordinary prose for one', () => {
		expect(isMachineCode('delivery_head_missing')).toBe(true);
		expect(isMachineCode('the provider was unreachable')).toBe(false);
		expect(isMachineCode('Retrying')).toBe(false);
	});

	it('offers one sentence for a request that never reached CORSIA', () => {
		// `String(error)` on a rejected fetch is a runtime's words — a
		// `TypeError: Failed to fetch` on screen is the same defect as a code.
		expect(isMachineCode(SERVER_UNREACHABLE)).toBe(false);
		expect(SERVER_UNREACHABLE).toMatch(/try again/i);
	});
});

describe('plainResponseError', () => {
	afterEach(() => { vi.restoreAllMocks(); });

	function refusal(body: unknown): Response {
		return { ok: false, status: 409, url: '/api/x', json: async () => body } as Response;
	}

	it('passes a server sentence through and swallows a code', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		await expect(plainResponseError(
			refusal({ detail: 'Queued messages cannot be truncated' }), 'fallback',
		)).resolves.toBe('Queued messages cannot be truncated');
		await expect(plainResponseError(
			refusal({ detail: 'provider_write_outcome_unknown' }), 'Could not send it',
		)).resolves.toBe('Could not send it');
	});

	it('never stringifies a detail object onto the screen', async () => {
		// This is the shape one caller used to JSON.stringify into a toast.
		vi.spyOn(console, 'error').mockImplementation(() => {});
		await expect(plainResponseError(
			refusal({ detail: { code: 'checkpoint_required', target_model: 'x' } }),
			'Could not do that. Try again.',
		)).resolves.toBe('Could not do that. Try again.');
	});

	it('falls back when the body is not JSON at all', async () => {
		const broken = {
			ok: false, status: 500, url: '/api/x',
			json: async () => { throw new SyntaxError('not json'); },
		} as unknown as Response;
		await expect(plainResponseError(broken, 'Could not do that. Try again.'))
			.resolves.toBe('Could not do that. Try again.');
	});
});
