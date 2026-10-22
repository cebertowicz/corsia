import { sourceIsReady, type ModelSource } from '$lib/conductorSources';
import { accountToSignIn, cancelCliInstall, cancelSignin, checkSignin, getCliInstall, linkCli, startCliInstall, startSignin, type Signin } from '$lib/conductorSourcesApi';
import { fetchCorsiaAgent } from '$lib/utils/conductor';
import { formatMoment } from '$lib/time';

type InstallFailureCause = 'connection' | 'site_refused' | 'download_unverified' | 'installer_failed';
type ConnectPhase = {
	phase: 'installing' | 'signing-in' | 'path-shadow' | 'failed'; installed?: boolean; accountId?: string; failed?: 'install' | 'signin';
	reason?: string | null; cause?: InstallFailureCause; ref?: string | null; shadowedBinary?: string;
	/** Sign in was pressed while installing: the sign-in starts the moment the install is done. */
	signInQueued?: boolean;
	/** A failed sign-in's stable cause, from the service or `unreachable` when no answer came; absent when unknown. */
	signinCause?: string;
	/** When the sign-in began waiting, so polling for it is bounded. */
	startedAt?: number;
};
export type SetupAttempt = { attempt: 'none' | 'installed' | 'install_failed' | 'signin_failed'; cause?: string; ref?: string };
export type RowView = {
	meta: string; ready?: boolean; busy?: boolean; spinning?: boolean; error?: boolean; detail?: string; reference?: string; reportable?: boolean;
	action?: { label: string; run: () => void; loading?: boolean };
	confirm?: { label: string; run: () => void }[];
	more?: { label: string; run: () => void }[];
};
export interface ConnectDeps {
	token: () => string | null; sources: () => ModelSource[]; refreshSources: (strict?: boolean) => Promise<void>;
	/** The person turned a tool off from its failed sign-in row: its pick is withdrawn. */
	turnOff?: (vendor: string) => void;
}
export const VENDOR_COPY: Record<string, { cli: string; uses: string; maker: string; account: string }> = {
	'claude-code': { cli: 'Claude Code', uses: 'Uses your Claude subscription.', maker: 'Anthropic', account: 'Claude' },
	codex: { cli: 'Codex', uses: 'Uses your ChatGPT subscription.', maker: 'OpenAI', account: 'ChatGPT' },
};
export const POLL_MS = 3000;
/** Longer than the service's own five-minute wait, so only a service that stopped answering reaches it. */
export const SIGNIN_WAIT_MS = 6 * 60 * 1000;
/** Failures where signing in with a different account is the fix. */
const ACCOUNT_CAUSES = ['signin_timed_out', 'no_access'];

/** Keep the API's stable failure code across the setup handoff. */
export function installCause(cause?: InstallFailureCause | null): InstallFailureCause {
	return cause ?? 'installer_failed';
}

/** The action that fixes a failure, plus a report ask only when a reference gives the report something to find. */
export function failureAction(action: string, ref?: string | null): string {
	return ref ? `${action}, or save a report.` : `${action}.`;
}

/** Keep refusal recovery distinct from retryable installer failures without exposing installer output. */
export function installFailureCopy(cli: string, cause?: InstallFailureCause | null, ref?: string | null) {
	if (installCause(cause) === 'site_refused') {
		// Without a reference there is nothing to report, so Retry is the only action left.
		return ref ? { meta: `Couldn’t download ${cli}. The download site refused the request. Save a report.`, reference: ref, retry: false }
			: { meta: `Couldn’t download ${cli}. The download site refused the request. Retry.`, reference: undefined, retry: true };
	}
	if (installCause(cause) === 'connection') {
		return { meta: `Couldn’t download ${cli}. Check your internet connection, then Retry.`, reference: ref ?? undefined, retry: true };
	}
	return { meta: `Couldn’t install ${cli}. ${failureAction('Retry', ref)}`, reference: ref ?? undefined, retry: true };
}

/** The sign-in's real cause in words, and whether a report would help diagnose it. */
export function signinFailureCopy(vendor: string, cause?: string | null, ref?: string | null): { meta: string; reportable: boolean } {
	const { cli, account, maker } = VENDOR_COPY[vendor] ?? { cli: vendor, account: vendor, maker: vendor };
	const again = failureAction('Sign in again', ref);
	if (cause === 'unreachable') return { meta: `CORSIA didn’t answer while starting the ${cli} sign-in. Sign in again.`, reportable: true };
	if (cause === 'agent_unavailable') return { meta: `CORSIA couldn’t reach its agent to start signing in. ${again}`, reportable: true };
	if (cause?.endsWith('_executable_missing')) return { meta: `CORSIA couldn’t find ${cli} on this computer. Sign in again to install it.`, reportable: true };
	if (cause?.endsWith('_signin_in_progress')) return { meta: `Another ${cli} sign-in is already open. Finish it in your browser, or sign in again.`, reportable: false };
	if (cause === 'vendor_login_failed') return { meta: `Signing in to ${cli} didn’t finish in the browser. Sign in again.`, reportable: false };
	if (cause === 'signin_timed_out') return { meta: `Didn’t finish signing in to ${cli}. Usually the account doesn’t include ${cli}, and your browser stops on a page from ${maker} instead of returning here. Try another account, or turn it off and use another option.`, reportable: false };
	if (cause === 'no_access') return { meta: `This ${account} account doesn’t include ${cli}. Try another account, or turn it off and use another option.`, reportable: false };
	if (cause === 'vendor_not_signed_in') return { meta: `${cli} didn’t report a signed-in ${account} account. Sign in again.`, reportable: false };
	if (cause === 'same_account') return { meta: `That ${account} account is already signed in here. To add another, sign in again with a different ${account} account.`, reportable: false };
	if (cause === 'signin_check_failed' || cause === 'vendor_status_failed') return { meta: `CORSIA couldn’t check the ${cli} sign-in. ${again}`, reportable: true };
	if (cause === 'vendor_login_unavailable' || cause === 'vendor_logout_unavailable') return { meta: `CORSIA couldn’t start the ${cli} sign-in. ${again}`, reportable: true };
	if (cause === 'prompt_unrecognized') return { meta: `${cli} asked something CORSIA didn’t expect. ${again}`, reportable: true };
	// The cause code itself goes to the report, never into the sentence.
	return { meta: `Couldn’t finish signing in to ${cli}. ${again}`, reportable: true };
}

/** The ready account's name for its row: an email, plan, or the vendor's own label. */
function readyAccount(source: ModelSource): string | undefined {
	return source.accounts?.find((candidate) => candidate.state === 'ready')?.label;
}

/** Install once a tool is picked, sign in on request; retain setup-only attempts for the handoff. */
export function createSetupConnect(deps: ConnectDeps) {
	let flow = $state<Record<string, ConnectPhase | undefined>>({});
	let attempts = $state<Record<string, SetupAttempt>>({ 'claude-code': { attempt: 'none' }, codex: { attempt: 'none' } });
	const active = $derived(Object.values(flow).some((entry) => entry?.phase === 'installing' || entry?.phase === 'signing-in'));
	function setConnect(vendor: string, next: ConnectPhase | undefined) {
		flow = { ...flow, [vendor]: next };
		if (next?.phase === 'failed') attempts = { ...attempts, [vendor]: { attempt: next.failed === 'signin' ? 'signin_failed' : 'install_failed', cause: next.failed === 'signin' ? next.signinCause ?? 'signin_failed' : installCause(next.cause), ref: next.ref ?? undefined } };
	}
	function signinFailed(vendor: string, result?: Signin | null, installed?: boolean, cause?: string) {
		setConnect(vendor, { phase: 'failed', failed: 'signin', installed, signinCause: result ? result.cause ?? result.status : cause, ref: result?.ref ?? null });
	}
	async function beginSignIn(source: ModelSource, installed = false) {
		const current = deps.token();
		if (!current) return;
		setConnect(source.source, { phase: 'signing-in', installed });
		// stop() clears the row when the tool is unpicked, even before the sign-in has an account.
		const stopped = () => flow[source.source]?.phase !== 'signing-in';
		let result: Signin | null = null;
		try {
			const lookup = await fetchCorsiaAgent(current);
			if (stopped()) return;
			if (lookup.kind !== 'target') { signinFailed(source.source, null, installed, 'agent_unavailable'); return; }
			const accountId = await accountToSignIn(current, source.source, source.name);
			if (stopped()) return;
			result = await startSignin(current, accountId, lookup.target.thread_id);
			if (stopped()) { await cancelSignin(current, accountId).catch(() => undefined); return; }
			if (['failed', 'unavailable', 'refused'].includes(result.status)) { signinFailed(source.source, result, installed); return; }
			setConnect(source.source, { phase: 'signing-in', installed, accountId, startedAt: Date.now() });
			await tickConnect();
		} catch (error) {
			// An unpicked tool stays cleared: its failure is never written back over the row stop() cleared.
			if (stopped()) return;
			// Only a request that got no answer is unreachable; a refusal's cause is unknown here.
			signinFailed(source.source, result, installed, error instanceof TypeError ? 'unreachable' : undefined);
		}
	}
	async function openSignInAgain(source: ModelSource) {
		const current = deps.token(), accountId = flow[source.source]?.accountId;
		if (current && accountId) await cancelSignin(current, accountId).catch(() => undefined);
		await beginSignIn(source, flow[source.source]?.installed);
	}
	async function afterInstalled(vendor: string, shadowedBinary: string | null = null) {
		const signInQueued = flow[vendor]?.signInQueued === true;
		attempts = { ...attempts, [vendor]: { attempt: 'installed' } };
		// A fresh check, never the stored list: the stored row can still say not installed.
		await deps.refreshSources(true);
		const source = deps.sources().find((candidate) => candidate.source === vendor);
		if (shadowedBinary) { setConnect(vendor, { phase: 'path-shadow', shadowedBinary }); return; }
		if (!source || sourceIsReady(source) || source.state === 'unverified') { setConnect(vendor, undefined); return; }
		if (source.state === 'not_installed') { setConnect(vendor, { phase: 'failed', failed: 'install' }); return; }
		if (signInQueued) await beginSignIn(source, true);
		else setConnect(vendor, undefined);
	}
	async function startInstall(source: ModelSource, signInQueued = false) {
		const current = deps.token();
		if (!current) return;
		setConnect(source.source, { phase: 'installing', signInQueued });
		try {
			const refusal = await startCliInstall(current, source.source);
			if (refusal === 'invisible_binary') await linkCli(current, source.source);
			if (refusal) { await afterInstalled(source.source); return; }
			await tickConnect();
		} catch { setConnect(source.source, { phase: 'failed', failed: 'install' }); }
	}
	/** A tool picked on the subscriptions screen: install it now unless it is already there. */
	async function install(source: ModelSource) {
		const entry = flow[source.source];
		if (source.state !== 'not_installed' || entry?.phase === 'installing' || entry?.phase === 'signing-in') return;
		await startInstall(source);
	}
	/** Sign in: once the install is done when one is running, after installing when the tool is missing. */
	async function signIn(source: ModelSource) {
		const entry = flow[source.source];
		if (entry?.phase === 'installing') { setConnect(source.source, { ...entry, signInQueued: true }); return; }
		if (source.state === 'not_installed') { await startInstall(source, true); return; }
		await beginSignIn(source);
	}
	async function cancelInstall(source: ModelSource) {
		const current = deps.token();
		if (!current) return;
		try {
			const install = await cancelCliInstall(current, source.source);
			if (install.state === 'done') await afterInstalled(source.source, install.path_shadowed_by);
			else if (install.state === 'failed') setConnect(source.source, { phase: 'failed', failed: 'install', reason: install.reason, cause: install.cause ?? undefined, ref: install.ref });
			else if (install.state !== 'running') setConnect(source.source, undefined);
		} catch { /* Retain pending state until the next authoritative check. */ }
	}
	/** An answer changed on the subscriptions screen: stop the install or sign-in it started. */
	async function stop(vendor: string) {
		const entry = flow[vendor], current = deps.token();
		if (entry?.phase === 'installing') {
			// A sign-in queued behind this install must not start if the install finishes anyway.
			setConnect(vendor, { ...entry, signInQueued: false });
			const source = deps.sources().find((candidate) => candidate.source === vendor);
			if (source) await cancelInstall(source);
		} else if (entry?.phase === 'signing-in') {
			setConnect(vendor, undefined);
			if (entry.accountId && current) await cancelSignin(current, entry.accountId).catch(() => undefined);
		}
	}
	let ticking = false;
	async function tickConnect() {
		const current = deps.token();
		if (!current || ticking) return;
		ticking = true;
		try {
			for (const [vendor, entry] of Object.entries(flow)) {
				if (entry?.phase === 'installing') {
					const install = await getCliInstall(current, vendor).catch(() => null);
					if (install?.state === 'done' || install?.state === 'idle') await afterInstalled(vendor, install.path_shadowed_by);
					else if (install?.state === 'cancelled') setConnect(vendor, undefined);
					else if (install?.state === 'failed') setConnect(vendor, { phase: 'failed', failed: 'install', reason: install.reason, cause: install.cause ?? undefined, ref: install.ref });
				} else if (entry?.phase === 'path-shadow') {
					const source = deps.sources().find((candidate) => candidate.source === vendor);
					if (source && sourceIsReady(source)) setConnect(vendor, undefined);
				} else if (entry?.phase === 'signing-in' && entry.accountId) {
					const signin = await checkSignin(current, entry.accountId).catch(() => null);
					const expired = entry.startedAt !== undefined && Date.now() - entry.startedAt > SIGNIN_WAIT_MS;
					if (signin?.status === 'completed') {
						// An outcome of null means the first read of the account's models is still running.
						if (signin.outcome === null && !expired) continue;
						await deps.refreshSources();
						if (signin.outcome === 'no_usable_model') signinFailed(vendor, null, entry.installed, 'no_access');
						else setConnect(vendor, undefined);
					} else if (signin && signin.status !== 'waiting') signinFailed(vendor, signin, entry.installed);
					else if (expired) {
						// The browser never came back: stop the vendor's login and stop asking about it.
						await cancelSignin(current, entry.accountId).catch(() => undefined);
						signinFailed(vendor, null, entry.installed, 'signin_timed_out');
					}
				}
			}
		} finally { ticking = false; }
	}
	function turnOff(vendor: string) {
		setConnect(vendor, undefined);
		deps.turnOff?.(vendor);
	}
	function connectRow(source: ModelSource): RowView {
		const copy = VENDOR_COPY[source.source], entry = flow[source.source];
		const signInAction = { label: 'Sign in', run: () => void signIn(source) };
		if (entry?.phase === 'installing') {
			return { meta: `Installing ${copy.cli}…`, busy: true, spinning: true, action: entry.signInQueued ? { label: 'Installing…', run: () => undefined, loading: true } : signInAction };
		}
		if (entry?.phase === 'signing-in') return { meta: 'Finish signing in in your browser.', busy: true, action: { label: 'Open sign-in again', run: () => void openSignInAgain(source) } };
		if (entry?.phase === 'failed' && entry.failed === 'signin') {
			const failure = signinFailureCopy(source.source, entry.signinCause, entry.ref);
			if (entry.signinCause && ACCOUNT_CAUSES.includes(entry.signinCause)) {
				return { meta: failure.meta, error: true, action: { label: 'Try another account', run: () => void beginSignIn(source, entry.installed) }, more: [{ label: 'Turn off', run: () => turnOff(source.source) }] };
			}
			return { meta: failure.meta, reference: entry.ref ?? undefined, error: true, reportable: failure.reportable, action: signInAction };
		}
		if (entry?.phase === 'failed') {
			const failure = installFailureCopy(copy.cli, entry.cause, entry.ref);
			return { meta: failure.meta, reference: failure.reference, error: true, reportable: true, action: failure.retry ? { label: 'Retry', run: () => void startInstall(source, true) } : undefined };
		}
		if (entry?.phase === 'path-shadow') return { meta: `An older ${copy.cli} at ${entry.shadowedBinary} runs instead of the one CORSIA installed. Remove it, then come back here.`, error: true, action: { label: 'Check again', run: () => void deps.refreshSources(true) } };
		if (source.limited) return { meta: source.resets_at ? `Signed in, but your ${copy.account} subscription has reached its usage limit until ${formatMoment(new Date(source.resets_at))}.` : `Signed in, but your ${copy.account} subscription has reached its usage limit.`, error: true };
		if (sourceIsReady(source)) {
			const account = readyAccount(source);
			return { meta: account ? `Ready · ${account}` : 'Ready', ready: true };
		}
		if (source.state === 'unverified' && source.cli_package_manager) {
			const command = source.cli_package_manager === 'homebrew' ? `brew reinstall ${source.source === 'claude-code' ? 'claude-code' : 'codex'}` : `npm install -g ${source.source === 'claude-code' ? '@anthropic-ai/claude-code' : '@openai/codex'}`;
			return { meta: `CORSIA can’t use this copy of ${copy.cli}. Reinstall it in Terminal with ${command}, then come back here.`, error: true, action: { label: 'Check again', run: () => void deps.refreshSources(true) } };
		}
		if (source.state === 'unverified') return { meta: source.reason ?? '', busy: !source.checked_at, action: source.checked_at ? { label: 'Check again', run: () => void deps.refreshSources(true) } : undefined };
		return { meta: copy.uses, action: signInAction };
	}
	return { get active() { return active; }, get attempts() { return attempts; }, tick: tickConnect, row: connectRow, install, signIn, stop, turnOff };
}
