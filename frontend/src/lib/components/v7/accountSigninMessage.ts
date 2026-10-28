import type { SigninResult } from '$lib/stores/vendorAccounts';

function readableCause(cause: string | null, cli: string | null): string {
	if (!cause) return 'Sign-in didn’t complete.';
	if (cause === 'vendor_login_failed') return 'Sign-in didn’t complete.';
	if (cause === 'signin_timed_out') return cli
		? `Couldn’t finish signing in. If your browser said this account doesn’t include ${cli}, sign in with another account or turn this account off. Otherwise, sign in again.`
		: 'Couldn’t finish signing in. Sign in again, or turn this account off.';
	if (cause === 'vendor_not_signed_in') return 'The vendor did not report this account as signed in.';
	if (cause === 'signin_check_failed') return 'CORSIA could not check the vendor sign-in.';
	if (cause === 'vendor_login_unavailable') return 'Sign-in is unavailable.';
	return cause.replaceAll('_', ' ');
}

/** Sign in again on an account the vendor still reports signed in starts no login: these two results end with the account as it was. */
export function isStillSignedIn(result: SigninResult): boolean {
	return result.status === 'completed' && (result.cause === 'already_signed_in' || result.cause === 'models_reload_failed');
}

/** The one line an account row shows for a sign-in attempt; `nameOf` names another account by id, `cli` is the tool the account signs in to. */
export function signinMessage(result: SigninResult, nameOf: (id: string | null | undefined) => string | null, signin: Record<string, SigninResult>, cli: string | null = null): string {
	if (result.status === 'waiting') return 'Waiting for sign-in in your browser';
	if (result.status === 'completed' && result.cause === 'already_signed_in') {
		const name = nameOf(result.account_id);
		return `${name ?? 'This account'} is still signed in. Its model list is up to date.`;
	}
	if (result.status === 'completed' && result.cause === 'models_reload_failed') {
		const name = nameOf(result.account_id);
		return `${name ?? 'This account'} is still signed in, but its model list couldn’t be reloaded. Use Sign in again to retry.${result.ref ? ` Reference: ${result.ref}` : ''}`;
	}
	if (result.status === 'completed') return 'Signed in';
	if (result.status === 'cancelled') return 'Sign-in cancelled';
	if (result.status === 'superseded') return 'Account changed before sign-in finished';
	if (result.status === 'unavailable') return 'Sign-in is unavailable';
	if (result.cause === 'same_account') {
		const name = nameOf(result.duplicate_of);
		return name ? `Already signed in as ${name}` : 'This account is already signed in';
	}
	if (result.status === 'refused') {
		const name = nameOf(result.held_by);
		const holder = result.held_by ? signin[result.held_by] : null;
		if (holder && holder.status !== 'waiting') return name ? `Sign-in was already in progress for ${name}` : 'Another sign-in was already in progress';
		return name ? `${name} is already signing in` : 'Another account is already signing in';
	}
	return readableCause(result.cause, cli);
}
