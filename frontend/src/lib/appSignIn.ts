import type { ConnectedApp } from '$lib/api/connectedApps';

// Every string here comes from the plain-language copy sheet (Sign-in section, row table and lede), with <app> filled in.

export const SIGN_IN_LEDE = "Signing in lets the agents that can use an app act as you in it. You sign in on the app's own page in your browser, so CORSIA never sees your password.";
export const NOT_ON_THIS_MAC = 'You can only sign in on the Mac CORSIA runs on. Open CORSIA there and press Sign in.';

/** Whether the row is waiting on, or in the middle of, a sign-in the person has to act on. A failed attempt stops needing them once the app is signed in. */
export function needsSignInAttention(app: ConnectedApp): boolean {
	const view = app.sign_in;
	return app.state === 'needs_sign_in' || view?.status === 'pending' || (view?.status === 'failed' && !view.signed_in);
}

/** The lede line shows while any row still needs a first sign-in. */
export function anyFirstSignIn(apps: ConnectedApp[]): boolean {
	return apps.some((app) => app.state === 'needs_sign_in' && app.sign_in && !app.sign_in.signed_in);
}

/** The row's state line when sign-in decides it; null when the app's own state line applies. */
export function signInStateLine(app: ConnectedApp, signedOutHere: boolean): string | null {
	const view = app.sign_in;
	if (!view) return null;
	if (view.status === 'pending') return 'Waiting for you in your browser';
	if (view.status === 'failed') return "Sign-in didn't finish";
	if (app.state === 'needs_sign_in') return signedOutHere ? 'Signed out' : 'Needs you to sign in';
	if (app.state === 'working' && view.signed_in && !view.account) return `Connected · Signed in${app.tool_count ? ` · ${app.tool_count} actions` : ''}`;
	return null;
}

/** The account a signed-in app row names in its title ("Figma — you@example.com"); null when the app is not signed in or did not say which. */
export function appAccount(app: ConnectedApp): string | null {
	const view = app.sign_in;
	return view?.signed_in && view.account ? view.account : null;
}

/** The app only lets in apps it has approved and nobody on this side can change that, so a second press would fail the same way. Remove is the way out. */
const NOBODY_CAN_FIX = new Set(['not_approved']);
/** Failures a Try again may not fix until someone else has acted (a client ID, the app's maker); the row says Sign in rather than promise a retry. */
const WAITS_ON_SOMEONE_ELSE = new Set(['permissions', 'address_mismatch']);

/** The button a failed sign-in's row shows, or null when pressing it again could not help. */
export function retryLabel(failureClass: string | undefined): 'Try again' | 'Sign in' | null {
	const name = failureClass ?? 'unknown';
	if (NOBODY_CAN_FIX.has(name)) return null;
	return WAITS_ON_SOMEONE_ELSE.has(name) ? 'Sign in' : 'Try again';
}

/** The sentence in the open row for a failed sign-in. `words` is the app's own text, shown only for an unknown class. */
export function failureText(name: string, failureClass: string): string {
	switch (failureClass) {
		case 'timed_out': return "Sign-in wasn't finished within 5 minutes, or the tab was closed.";
		case 'declined': return `The sign-in was declined on ${name}'s page. If you didn't mean to, try again.`;
		case 'not_allowed': return `${name} doesn't allow CORSIA to sign in. Ask whoever manages ${name} for your team to allow CORSIA in ${name}'s settings.`;
		case 'not_approved': return `${name} only lets in apps it has approved, and CORSIA isn't one of them. There's nothing to fix on your side. Press Remove to take it off this list.`;
		case 'permissions': return `${name} can't give CORSIA the access it needs. There's nothing to fix on your side; ${name}'s maker may need to update it.`;
		case 'app_error': return `${name} had a problem with the sign-in. Try again.`;
		case 'app_busy': return `${name} is busy. Try again in a few minutes.`;
		case 'address_mismatch':
			return `${name} didn't recognize CORSIA. It needs a code called a client ID. If someone else looks after ${name} for you, ask them for one. If you look after it yourself, you can make one in ${name}'s settings. Then tell CORSIA in chat what it is.`;
		default: return `Sign-in didn't finish, and CORSIA doesn't know why. Try again. If it keeps happening, show what ${name} said to whoever manages ${name} for your team. If that's you, try again later or contact ${name}.`;
	}
}

export const WAITING_TEXT = (name: string): string => `${name}'s sign-in page is open in your browser. Allow CORSIA there, then come back.`;
export const BROWSER_DIDNT_OPEN = 'Your browser didn\'t open. Copy the sign-in link and open it in a browser on this Mac.';
export const PROVIDER_NOT_TOLD_BEFORE = (name: string): string => `You're signed out here, but CORSIA couldn't reach ${name}, so ${name} may still list CORSIA as allowed. To remove it, open `;
export const PROVIDER_NOT_TOLD_AFTER = ' and remove CORSIA.';
export const SIGN_OUT_BODY = (name: string): string => `Agents will no longer be able to use ${name}, including in conversations already running. You can sign in again any time.`;

/** Whether a waiting row has anything to show from sign-in; an idle, signed-out app reserves no space for it. */
export function hasSignInToShow(app: ConnectedApp): boolean {
	const view = app.sign_in;
	return !!view && (view.status === 'pending' || view.status === 'failed' || view.signed_in);
}
