import type { SigninResult } from '$lib/stores/vendorAccounts';

function readableCause(cause: string | null): string {
	if (!cause) return 'Sign-in didn’t complete.';
	if (cause === 'vendor_login_failed') return 'Sign-in didn’t complete.';
	if (cause === 'vendor_not_signed_in') return 'The vendor did not report this account as signed in.';
	if (cause === 'signin_check_failed') return 'CORSIA could not check the vendor sign-in.';
	if (cause === 'vendor_login_unavailable') return 'Sign-in is unavailable.';
	return cause.replaceAll('_', ' ');
}

/** The one line an account row shows for a sign-in attempt; `nameOf` names another account by id. */
export function signinMessage(result: SigninResult, nameOf: (id: string | null | undefined) => string | null, signin: Record<string, SigninResult>): string {
	if (result.status === 'waiting') return 'Waiting for sign-in in your browser';
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
	return readableCause(result.cause);
}
