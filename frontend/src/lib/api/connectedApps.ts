import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { plainResponseError } from '$lib/plainError';
import { signInStateLine } from '$lib/appSignIn';

export type AppState = 'awaiting_approval' | 'connecting' | 'working' | 'needs_token' | 'token_rejected' | 'needs_sign_in' | 'disconnected' | 'failed' | 'expired';

export interface AppKeyRef {
	/** Display-only declared reference; it never contains a key value. */
	name: string;
	kind: 'env' | 'header';
	/** Managed key-store entry name, for navigation to the existing Keys card. */
	entry: string;
	set: boolean;
}

export interface ConnectedApp {
	id: string;
	name: string;
	state: AppState | string;
	revision: number;
	tool_count?: number;
	failure_class?: string | null;
	reach: { mode: 'all' | 'selected'; project_ids: string[]; outside_projects: boolean };
	/** `stdio` is an app that runs on this Mac; `http` is an app at a web address. */
	transport?: 'stdio' | 'http' | null;
	/** Display label only: the last segment of the Claude Code folder the app was set up for. Plain text. */
	folder?: string;
	/** The server's notice for an app that runs on this Mac, on a request waiting for approval and on the answer to approving it. */
	notice?: string;
	/** Only on the answer to Decline when it deleted a new request: the row is gone. */
	removed?: boolean;
	/** Present for an app at a web address. Never holds a token or the sign-in link. */
	sign_in?: SignInView;
	/** Declared key metadata only; values never leave the key store. */
	key_refs?: AppKeyRef[];
}

export type AppReach = ConnectedApp['reach'];

export const ALL_PROJECTS_REACH: AppReach = { mode: 'all', project_ids: [], outside_projects: false };

/** Why a Google account's sign-in is gone or was just revoked. Only the last four show the manual Google Account step. */
export type RevocationCause =
	| 'revoked'
	| 'not_signed_in'
	| 'provider_unreachable'
	| 'provider_timeout'
	| 'provider_rejected'
	| 'provider_unexpected';

export type AccountState = 'connected' | 'needs_sign_in' | 'signed_out';

/** An account's reach: an app's, or `mode: 'corsia'` (Only CORSIA), which no save ever writes. */
export type AccountReach = Omit<AppReach, 'mode'> & { mode: AppReach['mode'] | 'corsia' };

/** The one test for Only CORSIA. */
export function isOnlyCorsia(reach: AccountReach): boolean {
	return reach.mode === 'corsia';
}

/** One signed-in provider account (today Google). Never holds a credential, a key name, a token or provider text. */
export interface ConnectedAccount {
	kind: 'account';
	id: string;
	provider: 'google';
	/** Display only: the address can change at Google. */
	address: string;
	/** Stable ids; the screen words them: `mail` is Email, `calendar` Calendar, `files` Files. */
	capabilities: ('mail' | 'calendar' | 'files')[];
	state: AccountState;
	reach: AccountReach;
}

/** A hosted or local app row, as `/api/mcp/servers` returns it, tagged for the union. */
export type HostedAppRow = ConnectedApp & { kind: 'hosted_app' };
export type ConnectedAppsRow = HostedAppRow | ConnectedAccount;

/** What the screen shows after Sign out or Remove. Only `MANUAL_STEP_CAUSES` show the manual Google Account step. */
export const MANUAL_STEP_CAUSES: readonly RevocationCause[] = ['provider_unreachable', 'provider_timeout', 'provider_rejected', 'provider_unexpected'];

export function needsManualGoogleStep(cause: RevocationCause): boolean {
	return MANUAL_STEP_CAUSES.includes(cause);
}

export interface AccountSignOutResult { row: ConnectedAccount; revocation: RevocationCause }
export interface AccountRemoveResult { id: string; removed: true; revocation: RevocationCause }

export interface SignInView {
	status: 'idle' | 'pending' | 'failed';
	signed_in: boolean;
	failure_class?: string;
	expires_at?: string;
	account?: string;
}

/** The shared row/result status line; sign-in state takes precedence when it has one. */
export function connectedAppStateLine(app: ConnectedApp, signedOutHere = false): string {
	const signIn = signInStateLine(app, signedOutHere);
	if (signIn) return signIn;
	switch (app.state) {
		case 'awaiting_approval': return 'Waiting for your approval';
		case 'connecting': return 'Connecting…';
		case 'working': return `Connected${app.tool_count ? ` · ${app.tool_count} actions` : ''}`;
		case 'needs_token': return 'Needs a key';
		case 'token_rejected': return 'Key not accepted';
		case 'needs_sign_in': return 'Needs you to sign in';
		case 'disconnected': return 'Disconnected';
		case 'expired': return 'Request expired';
		case 'failed': return `Couldn't connect: ${({ cannot_start: "app didn't start", timed_out: 'no response', protocol_error: 'not compatible', name_collision: 'clashes with another app' }[app.failure_class ?? ''] ?? 'unknown reason')}`;
		default: return app.state;
	}
}

export const needsTokenBody = (name: string): string => `${name} needs a key before agents can use it: a code ${name} gives you so other apps can use your account (${name} may call it a token or an API key). Create one in your ${name} settings.`;

export interface SignInStart { authorization_url: string; expires_at: string; reopened: boolean }
export interface SignInStatus extends Partial<Pick<SignInView, 'status' | 'failure_class' | 'expires_at'>> { authorization_url?: string; words?: string }
export interface SignOutResult { id: string; had_sign_in: boolean; provider_told: boolean; provider_url: string | null; state: string }

function url(path: string): string {
	const auth = get(token);
	return `${path}?token=${encodeURIComponent(auth ?? '')}`;
}

const ACCOUNT_FAILED = "Couldn't change this Google account. Try again.";

async function response<T>(path: string, init?: RequestInit, fallback = 'Could not update this app. Try again.'): Promise<T> {
	const result = await fetch(url(path), init);
	if (!result.ok) throw new Error(await plainResponseError(result, fallback));
	return result.json() as Promise<T>;
}

export async function loadConnectedAppsList(): Promise<{ servers: ConnectedApp[]; signInHere: boolean }> {
	const result = await response<{ servers: ConnectedApp[]; sign_in_here?: boolean }>('/api/mcp/servers');
	return { servers: result.servers, signInHere: result.sign_in_here ?? false };
}

export async function loadConnectedApps(): Promise<ConnectedApp[]> {
	return (await loadConnectedAppsList()).servers;
}

/** The one list the Connected apps screen renders: Google accounts first, then apps. */
export async function loadConnectedAppsRows(): Promise<{ rows: ConnectedAppsRow[]; signInHere: boolean }> {
	const result = await response<{ servers: ConnectedApp[]; accounts?: ConnectedAccount[]; sign_in_here?: boolean }>('/api/mcp/servers');
	const accounts = (result.accounts ?? []).map((account): ConnectedAccount => ({ ...account, kind: 'account' }));
	const apps = result.servers.map((app): HostedAppRow => ({ ...app, kind: 'hosted_app' }));
	return { rows: [...accounts, ...apps], signInHere: result.sign_in_here ?? false };
}

/** Starts a sign-in for this account in the browser on this Mac; the answer holds the link to open. */
export function startAccountSignIn(id: string): Promise<{ url: string; account_id: string }> {
	return response('/api/google/connect', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ account_id: id }),
	}, "Couldn't start Google sign-in. Try again.");
}

export function signOutOfAccount(id: string): Promise<AccountSignOutResult> {
	return response(`/api/mcp/accounts/${encodeURIComponent(id)}/sign-out`, { method: 'POST' }, ACCOUNT_FAILED);
}

export function removeAccount(id: string): Promise<AccountRemoveResult> {
	return response(`/api/mcp/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' }, ACCOUNT_FAILED);
}

export function saveAccountReach(id: string, reach: AppReach): Promise<{ row: ConnectedAccount }> {
	return response(`/api/mcp/accounts/${encodeURIComponent(id)}/reach`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ reach }),
	}, ACCOUNT_FAILED);
}

/** Starting failed in a way the screen names (a fixed class), not with the server's text. */
export class SignInStartError extends Error {
	constructor(readonly failureClass: string) { super(failureClass); }
}

export async function startAppSignIn(id: string): Promise<SignInStart> {
	const result = await fetch(url(`/api/mcp/servers/${encodeURIComponent(id)}/sign-in`), { method: 'POST' });
	if (result.ok) return result.json() as Promise<SignInStart>;
	const body = await result.clone().json().catch(() => null);
	if (typeof body?.detail?.failure_class === 'string') throw new SignInStartError(body.detail.failure_class);
	throw new Error(await plainResponseError(result, "Couldn't start the sign-in. Try again."));
}

export function appSignInStatus(id: string): Promise<SignInStatus> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/sign-in`);
}

export function cancelAppSignIn(id: string): Promise<{ cancelled: boolean }> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/sign-in`, { method: 'DELETE' });
}

export function signOutOfApp(id: string): Promise<SignOutResult> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/sign-out`, { method: 'POST' });
}

/** A fixed connected-app startup class that the form can describe without exposing a server error. */
export class ConnectAppError extends Error {
	constructor(readonly failureClass: string) { super(failureClass); }
}

export async function createConnectedApp(input: Pick<ConnectedApp, 'name' | 'reach'> & { config: Record<string, unknown> }): Promise<ConnectedApp> {
	const result = await fetch(url('/api/mcp/servers'), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ ...input, key_refs: [] }),
	});
	if (result.ok) return result.json() as Promise<ConnectedApp>;
	const body = await result.clone().json().catch(() => null);
	if (typeof body?.detail?.failure_class === 'string') throw new ConnectAppError(body.detail.failure_class);
	throw new Error('Couldn\'t connect. Try again.');
}

export function approveConnectedApp(app: Pick<ConnectedApp, 'id' | 'revision'>, reach?: ConnectedApp['reach']): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(app.id)}/approve`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ expected_revision: app.revision, ...(reach ? { reach } : {}) }),
	});
}

export function declineConnectedApp(id: string): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/decline`, { method: 'POST' });
}

export function reconnectConnectedApp(id: string): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}/reconnect`, { method: 'POST' });
}

export function removeConnectedApp(id: string): Promise<void> {
	return response(`/api/mcp/servers/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export function saveConnectedAppReach(app: ConnectedApp, reach: ConnectedApp['reach']): Promise<ConnectedApp> {
	return response(`/api/mcp/servers/${encodeURIComponent(app.id)}/reach`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ reach, expected_revision: app.revision }),
	});
}
