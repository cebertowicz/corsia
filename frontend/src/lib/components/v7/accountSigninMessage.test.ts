import { describe, expect, it } from 'vitest';
import { signinMessage } from './accountSigninMessage';
import type { SigninResult } from '$lib/stores/vendorAccounts';

const timedOut = { account_id: 'work', route: 'browser', status: 'failed', cause: 'signin_timed_out' } as SigninResult;
const nameOf = () => null;

describe('an unavailable sign-in on an account row', () => {
	it('shows a reference when present', () => {
		const unavailable = { account_id: 'work', route: null, status: 'unavailable', cause: 'vendor_status_unreadable', ref: 'R-7K2M' } as SigninResult;
		expect(signinMessage(unavailable, nameOf, {})).toBe('Sign-in is unavailable. Reference: R-7K2M');
		expect(signinMessage({ ...unavailable, ref: undefined }, nameOf, {})).toBe('Sign-in is unavailable');
	});
});

describe('a timed-out sign-in on an account row', () => {
	it.each(['Claude Code', 'Codex'])('says what the browser may have shown for %s, and what to do either way', (cli) => {
		expect(signinMessage(timedOut, nameOf, {}, cli)).toBe(`Couldn’t finish signing in. If your browser said this account doesn’t include ${cli}, sign in with another account or remove this one. Otherwise, sign in again.`);
	});

	it('does not name a tool it cannot name', () => {
		expect(signinMessage(timedOut, nameOf, {})).toBe('Couldn’t finish signing in. Sign in again, or remove this account.');
	});
});
