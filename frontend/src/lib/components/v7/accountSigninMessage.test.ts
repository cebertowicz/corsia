import { describe, expect, it } from 'vitest';
import { signinMessage } from './accountSigninMessage';
import type { SigninResult } from '$lib/stores/vendorAccounts';

const timedOut = { account_id: 'work', route: 'browser', status: 'failed', cause: 'signin_timed_out' } as SigninResult;
const nameOf = () => null;

it('shows an already-signed-in result as signed in', () => {
	expect(signinMessage({ account_id: 'work', route: null, status: 'already_signed_in', cause: null }, nameOf, {})).toBe('Signed in');
});

it('shows the account and reference when its model refresh fails', () => {
	expect(signinMessage({ account_id: 'work', route: null, status: 'model_refresh_failed', cause: 'account_refresh_failed', ref: 'signin-123' }, () => 'Work', {})).toBe('Work is still signed in, but its model list couldn’t be reloaded. Reference: signin-123');
});

describe('a timed-out sign-in on an account row', () => {
	it.each(['Claude Code', 'Codex'])('says what the browser may have shown for %s, and what to do either way', (cli) => {
		expect(signinMessage(timedOut, nameOf, {}, cli)).toBe(`Couldn’t finish signing in. If your browser said this account doesn’t include ${cli}, sign in with another account or turn this account off. Otherwise, sign in again.`);
	});

	it('does not name a tool it cannot name', () => {
		expect(signinMessage(timedOut, nameOf, {})).toBe('Couldn’t finish signing in. Sign in again, or turn this account off.');
	});
});
