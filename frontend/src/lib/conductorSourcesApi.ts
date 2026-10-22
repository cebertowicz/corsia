import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { loadModels } from '$lib/stores/modelsLoad';
import { loadThreadVendorAccounts } from '$lib/stores/threadVendorAccounts';
import { PIN_FAILED } from '$lib/stores/threadAccountPin';
import { SERVER_UNREACHABLE } from '$lib/plainError';

/**
 * The calls the first-run sources panel makes beyond reading the sources: the CLI install and
 * link routes, the conductor's account bind step, and the added-account sign-in. Each takes
 * the caller's token; none reads or writes the login the person's own CLI uses.
 */

const headers = (t: string, json = false): Record<string, string> => ({
	Authorization: `Bearer ${t}`, ...(json ? { 'Content-Type': 'application/json' } : {}),
});

export type BindReason = 'not_ready' | 'limited' | 'no_usable_model' | 'unchecked' | 'stale'
	| 'not_permitted' | 'corsia_agent_started' | 'account_missing';
export interface BindResult { bound: boolean; reason: BindReason | null }

export async function bindConductorAccount(t: string, threadId: string, accountId: string): Promise<BindResult> {
	const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/corsia-agent-account`, {
		method: 'POST', headers: headers(t, true), body: JSON.stringify({ account_id: accountId }),
	});
	if (!response.ok) throw new Error(`bind ${response.status}`);
	const body = await response.json();
	return { bound: body?.bound === true, reason: body?.reason ?? null };
}

/**
 * A picker account row on the model-less conductor: the bind step takes the vendor from the
 * account, where the pin route would refuse for want of a model. The binding and the catalog are
 * read again afterwards, as a pin does.
 */
export async function bindConductorFromPicker(threadId: string, accountId: string): Promise<{ ok: true } | { ok: false; message: string }> {
	const t = get(token);
	if (!t) return { ok: false, message: PIN_FAILED };
	try {
		const result = await bindConductorAccount(t, threadId, accountId);
		if (!result.bound) return { ok: false, message: PIN_FAILED };
	} catch {
		return { ok: false, message: SERVER_UNREACHABLE };
	}
	await Promise.all([loadThreadVendorAccounts(threadId, `bind-${accountId}-${Date.now()}`), loadModels()]);
	return { ok: true };
}

export type LinkOutcome = 'linked' | 'already_linked' | 'not_found' | 'timed_out' | 'conflict';

/** Link an installed CLI where CORSIA's service can see it; `outcome` says why a failed link failed. */
export async function linkCli(t: string, vendor: string): Promise<{ ok: boolean; outcome: LinkOutcome | null }> {
	const response = await fetch(`/api/providers/cli/${vendor}/link`, { method: 'POST', headers: headers(t) });
	const body = await response.json().catch(() => null);
	return { ok: response.ok && body?.ok === true, outcome: body?.outcome ?? null };
}

export type CliInstallState = 'idle' | 'running' | 'done' | 'failed' | 'cancelled';
export interface CliInstall { state: CliInstallState; reason: string | null; cause: 'connection' | 'site_refused' | 'download_unverified' | 'installer_failed' | null; ref: string | null; path_shadowed_by: string | null }

function cliInstallOf(body: Record<string, unknown> | null): CliInstall {
	return {
		state: (body?.state as CliInstallState) ?? 'failed',
		reason: (body?.reason as string | null) ?? null,
		cause: ['connection', 'site_refused', 'download_unverified', 'installer_failed'].includes(String(body?.cause)) ? body?.cause as CliInstall['cause'] : null,
		ref: (body?.ref as string | null) ?? null,
		path_shadowed_by: (body?.path_shadowed_by as string | null) ?? null,
	};
}

export type CliInstallRefusal = 'already_installed' | 'invisible_binary' | 'not_installed' | 'package_managed';

/**
 * Start one vendor CLI installation; callers poll its bounded per-vendor flight. A CLI that is
 * already there is not a failure: the refusal says which, and the card's state is what's stale.
 */
async function startCliJob(t: string, vendor: string, operation: 'install' | 'update'): Promise<CliInstallRefusal | null> {
	const response = await fetch(`/api/providers/cli/${encodeURIComponent(vendor)}/${operation}`, {
		method: 'POST', headers: headers(t),
	});
	if (response.status === 409) {
		const reason = (await response.json().catch(() => null))?.reason;
		if (reason === 'already_installed' || reason === 'invisible_binary' || reason === 'not_installed' || reason === 'package_managed') return reason;
	}
	if (!response.ok) throw new Error(`CLI ${operation} ${response.status}`);
	return null;
}

/** Start one vendor CLI installation; callers poll its bounded per-vendor flight. */
export function startCliInstall(t: string, vendor: string): Promise<CliInstallRefusal | null> {
	return startCliJob(t, vendor, 'install');
}

/** Rerun a vendor's official installer for a discovered-but-unusable CLI. */
export function startCliUpdate(t: string, vendor: string): Promise<CliInstallRefusal | null> {
	return startCliJob(t, vendor, 'update');
}

/** Read a vendor CLI installation's current state without starting another installer. */
export async function getCliInstall(t: string, vendor: string): Promise<CliInstall> {
	const response = await fetch(`/api/providers/cli/${encodeURIComponent(vendor)}/install`, { headers: headers(t) });
	if (!response.ok) throw new Error(`CLI install ${response.status}`);
	return cliInstallOf(await response.json().catch(() => null));
}

/** Stop a running vendor CLI installation; the answer is the state it settled in ('done' when it had just finished). */
export async function cancelCliInstall(t: string, vendor: string): Promise<CliInstall> {
	const response = await fetch(`/api/providers/cli/${encodeURIComponent(vendor)}/install`, { method: 'DELETE', headers: headers(t) });
	if (!response.ok) throw new Error(`CLI install cancel ${response.status}`);
	return cliInstallOf(await response.json().catch(() => null));
}

export type SigninStatus = 'waiting' | 'completed' | 'failed' | 'refused' | 'cancelled' | 'superseded' | 'unavailable';
export type SigninOutcome = 'ready' | 'limited' | 'no_usable_model' | 'unchecked' | 'bind_refused';
export interface Signin {
	account_id: string;
	status: SigninStatus;
	cause: string | null;
	outcome: SigninOutcome | null;
	bind_reason: string | null;
	/** A failed sign-in's public reference, matching its service-log line. */
	ref?: string | null;
}

function signinOf(body: Record<string, unknown> | null, accountId: string): Signin {
	return {
		account_id: String(body?.account_id ?? accountId),
		status: (body?.status as SigninStatus) ?? 'failed',
		cause: (body?.cause as string | null) ?? null,
		outcome: (body?.outcome as SigninOutcome | null) ?? null,
		bind_reason: (body?.bind_reason as string | null) ?? null,
		ref: typeof body?.ref === 'string' ? body.ref : null,
	};
}

/**
 * The account a sign-in runs on: the vendor's own added account that is still signed out, else
 * a new one. The built-in Default is never used; it keeps the person's CLI login.
 */
export async function accountToSignIn(t: string, provider: string, name: string): Promise<string> {
	const listed = await fetch(`/api/vendor-accounts?provider=${encodeURIComponent(provider)}`, { headers: headers(t) });
	if (listed.ok) {
		const body = await listed.json();
		const reusable = (body?.accounts ?? []).find((account: { is_default: boolean; state: string }) =>
			!account.is_default && account.state === 'signed_out');
		if (reusable) return reusable.id;
	}
	const added = await fetch('/api/vendor-accounts', {
		method: 'POST', headers: headers(t, true), body: JSON.stringify({ provider, name }),
	});
	if (!added.ok) throw new Error(`add account ${added.status}`);
	return (await added.json()).id;
}

/** Start the browser sign-in; the backend binds the conductor to the account if it comes back Ready. */
export async function startSignin(t: string, accountId: string, bindThreadId: string): Promise<Signin> {
	const response = await fetch(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin`, {
		method: 'POST', headers: headers(t, true), body: JSON.stringify({ bind_thread_id: bindThreadId }),
	});
	if (!response.ok) throw new Error(`signin ${response.status}`);
	return signinOf(await response.json().catch(() => null), accountId);
}

export async function checkSignin(t: string, accountId: string): Promise<Signin> {
	const response = await fetch(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin/check`, {
		method: 'POST', headers: headers(t),
	});
	if (!response.ok) throw new Error(`signin check ${response.status}`);
	return signinOf(await response.json().catch(() => null), accountId);
}

export async function cancelSignin(t: string, accountId: string): Promise<Signin> {
	const response = await fetch(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin`, {
		method: 'DELETE', headers: headers(t),
	});
	if (!response.ok) throw new Error(`signin cancel ${response.status}`);
	return signinOf(await response.json().catch(() => null), accountId);
}

/** A lifecycle action's outcome; a failure the service logged carries its reference. */
export type LifecycleResult = { ok: boolean; ref?: string };

/** Start or stop the person's own local server through the existing lifecycle route. */
async function localServerLifecycle(t: string, action: 'start' | 'stop'): Promise<LifecycleResult> {
	const listed = await fetch('/api/settings/network-servers', { headers: headers(t) });
	if (!listed.ok) return { ok: false };
	const servers: { id: string; provider: string; kind?: string | null; status: string }[] = (await listed.json())?.servers ?? [];
	const server = servers.find((row) => row.provider === 'omlx' && row.kind !== 'external' && row.status !== 'disabled');
	if (!server) return { ok: false };
	const response = await fetch(`/api/settings/network-servers/${encodeURIComponent(server.id)}/lifecycle/${action}`, {
		method: 'POST', headers: headers(t),
	});
	if (!response.ok) return { ok: false };
	const body = await response.json().catch(() => null);
	return { ok: body?.ok !== false, ref: typeof body?.ref === 'string' ? body.ref : undefined };
}

/** Start the person's stopped local server through the existing lifecycle route. */
export async function startLocalServer(t: string): Promise<boolean> {
	return (await localServerLifecycle(t, 'start')).ok;
}

/** Turn the person's local server off: the setup toggle's off once the local model is Ready. */
export async function stopLocalServer(t: string): Promise<LifecycleResult> {
	return localServerLifecycle(t, 'stop');
}

export interface StarterResult { ok: boolean; in_progress?: boolean; reason?: 'not_your_omlx' | 'mac_cannot_run'; message?: string }

/** Ask the person's own oMLX to download the starter model; it writes no settings. */
export async function addStarterModel(t: string): Promise<StarterResult> {
	const response = await fetch('/api/providers/local/starter', { method: 'POST', headers: headers(t) });
	if (!response.ok) throw new Error(`starter ${response.status}`);
	return response.json();
}
