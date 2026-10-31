import { sourceIsReady, type ModelSource } from '$lib/conductorSources';
import { accountToSignIn, cancelCliInstall, cancelSignin, checkSignin, getCliInstall, linkCli, startCliInstall, startSignin, type Signin } from '$lib/conductorSourcesApi';
import { fetchCorsiaAgent } from '$lib/utils/conductor';
import { formatMoment } from '$lib/time';

type InstallFailureCause = 'connection' | 'site_refused' | 'download_unverified' | 'installer_failed';
type ConnectPhase = {
	phase: 'awaiting-source' | 'installing' | 'signing-in' | 'path-shadow' | 'failed'; accountId?: string; failed?: 'install' | 'signin';
	reason?: string | null; cause?: InstallFailureCause; ref?: string | null; shadowedBinary?: string;
	/** A failed sign-in's stable cause, from the service or `unreachable` when no answer came; absent when unknown. */
	signinCause?: string;
	checkingModels?: boolean;
};
export type SetupAttempt = { attempt: 'none' | 'installed' | 'install_failed' | 'signin_failed'; cause?: string; ref?: string };
export type RowView = {
	meta: string; ready?: boolean; busy?: boolean; spinning?: boolean; error?: boolean; detail?: string; reference?: string; reportable?: boolean;
	action?: { label: string; run: () => void; loading?: boolean };
	confirm?: { label: string; run: () => void }[];
	more?: { label: string; run: () => void }[];
	/** This account can't run the tool: the screen offers its own switch to turn the tool off. */
	canTurnOff?: boolean;
};
export interface ConnectDeps { token: () => string | null; sources: () => ModelSource[]; refreshSources: (strict?: boolean) => Promise<void> }
export const VENDOR_COPY: Record<string, { cli: string; uses: string; maker: string; account: string }> = {
	'claude-code': { cli: 'Claude Code', uses: 'Uses your Claude subscription.', maker: 'Anthropic', account: 'Claude' },
	codex: { cli: 'Codex', uses: 'Uses your ChatGPT subscription.', maker: 'OpenAI', account: 'ChatGPT' },
};
export const POLL_MS = 3000;

/** Keep the API's stable failure code across the setup handoff. */
export function installCause(cause?: InstallFailureCause | null): InstallFailureCause {
	return cause ?? 'installer_failed';
}

/** The action that fixes a failure, plus a report ask only when a reference gives the report something to find. */
export function failureAction(action: string, ref?: string | null): string {
	return ref ? `${action}, or save a report.` : `${action}.`;
}

/** Keep refusal recovery distinct from retryable installer failures without exposing installer output. */
export function installFailureCopy(cli: string, cause?: InstallFailureCause | null, ref?: string | null, maker?: string) {
	if (installCause(cause) === 'site_refused') {
		// Retry stays beside the report: the line tells the person to try again.
		return { meta: `${maker ?? 'The maker'}’s site refused the download. Try again, or install it yourself.`, reference: ref ?? undefined, retry: true };
	}
	if (installCause(cause) === 'connection') {
		return { meta: `Couldn’t download ${cli}. Check your internet connection, then Retry.`, reference: ref ?? undefined, retry: true };
	}
	return { meta: `Couldn’t install ${cli}. ${failureAction('Retry', ref)}`, reference: ref ?? undefined, retry: true };
}

/** The sign-in's real cause in words, and whether a report would help diagnose it. */
export function signinFailureCopy(vendor: string, cause?: string | null, ref?: string | null): { meta: string; reportable: boolean } {
	const { cli, account } = VENDOR_COPY[vendor] ?? { cli: vendor, account: vendor };
	const retry = ref ? 'Press Sign in to try again, or save a report.' : 'Press Sign in to try again.';
	if (cause?.endsWith('_executable_missing')) return { meta: `CORSIA couldn’t find ${cli} on this computer. Sign in again to install it.`, reportable: true };
	if (cause?.endsWith('_signin_in_progress')) return { meta: `Another ${cli} sign-in is already open. Finish it in your browser, or sign in again.`, reportable: false };
	if (cause === 'signin_timed_out') return { meta: `Couldn’t finish signing in to ${cli}. If your browser said this account doesn’t include ${cli}, sign in with another account or turn ${cli} off. Otherwise, sign in again.`, reportable: false };
	if (cause === 'vendor_login_failed') return { meta: 'Sign-in didn’t finish in your browser. Press Sign in to try again.', reportable: false };
	if (cause === 'vendor_not_signed_in') return { meta: `${cli} didn’t report a signed-in ${account} account. Sign in again.`, reportable: false };
	if (cause === 'same_account') return { meta: `That ${account} account is already signed in here. To add another, sign in again with a different ${account} account.`, reportable: false };
	// Every other cause is CORSIA’s own plumbing: the person gets one line and the report, never the cause code.
	return { meta: `Sign-in didn’t finish. ${retry}`, reportable: true };
}

/** The ready account's name for its row: an email, plan, or the vendor's own label. */
function readyAccount(source: ModelSource): string | undefined {
	return source.accounts?.find((candidate) => candidate.state === 'ready')?.label;
}

/** The service records a signed-in account for this tool and none that can run a model. */
function hasNoUsableModel(source: ModelSource): boolean {
	const accounts = source.accounts ?? [];
	return accounts.some((account) => account.state === 'no_usable_model') && accounts.every((account) => account.state === 'no_usable_model' || account.state === 'signed_out');
}

/** Install once a tool is picked, sign in on request; retain setup-only attempts for the handoff. */
export function createSetupConnect(deps: ConnectDeps) {
	let flow = $state<Record<string, ConnectPhase | undefined>>({});
	const starting = new Set<string>();
	let attempts = $state<Record<string, SetupAttempt>>({ 'claude-code': { attempt: 'none' }, codex: { attempt: 'none' } });
	const active = $derived(Object.values(flow).some((entry) => entry?.phase === 'awaiting-source' || entry?.phase === 'installing' || entry?.phase === 'signing-in'));
	function setConnect(vendor: string, next: ConnectPhase | undefined) {
		flow = { ...flow, [vendor]: next };
		if (next?.phase === 'failed') attempts = { ...attempts, [vendor]: { attempt: next.failed === 'signin' ? 'signin_failed' : 'install_failed', cause: next.failed === 'signin' ? next.signinCause ?? 'signin_failed' : installCause(next.cause), ref: next.ref ?? undefined } };
	}
	function signinFailed(vendor: string, result?: Signin | null, cause?: string) {
		setConnect(vendor, { phase: 'failed', failed: 'signin', signinCause: result ? result.cause ?? result.status : cause, ref: result?.ref ?? null });
	}
	async function beginSignIn(source: ModelSource) {
		const current = deps.token();
		if (!current) return;
		setConnect(source.source, { phase: 'signing-in' });
		// stop() clears the row when the tool is unpicked, even before the sign-in has an account.
		const stopped = () => flow[source.source]?.phase !== 'signing-in';
		let result: Signin | null = null;
		try {
			const lookup = await fetchCorsiaAgent(current);
			if (stopped()) return;
			if (lookup.kind !== 'target') { signinFailed(source.source, null, 'agent_unavailable'); return; }
			const accountId = await accountToSignIn(current, source.source, source.name);
			if (stopped()) return;
			result = await startSignin(current, accountId, lookup.target.thread_id);
			if (stopped()) { await cancelSignin(current, accountId).catch(() => undefined); return; }
			if (['failed', 'unavailable', 'refused'].includes(result.status)) { signinFailed(source.source, result); return; }
			setConnect(source.source, { phase: 'signing-in', accountId });
			await tickConnect();
		} catch (error) {
			// An unpicked tool stays cleared: its failure is never written back over the row stop() cleared.
			if (stopped()) return;
			// Only a request that got no answer is unreachable; a refusal's cause is unknown here.
			signinFailed(source.source, result, error instanceof TypeError ? 'unreachable' : undefined);
		}
	}
	async function openSignInAgain(source: ModelSource) {
		const current = deps.token(), accountId = flow[source.source]?.accountId;
		if (current && accountId) await cancelSignin(current, accountId).catch(() => undefined);
		await beginSignIn(source);
	}
	async function afterInstalled(vendor: string, shadowedBinary: string | null = null) {
		attempts = { ...attempts, [vendor]: { attempt: 'installed' } };
		// A fresh check, never the stored list: the stored row can still say not installed.
		await deps.refreshSources(true);
		const source = deps.sources().find((candidate) => candidate.source === vendor);
		if (shadowedBinary) { setConnect(vendor, { phase: 'path-shadow', shadowedBinary }); return; }
		if (!source || sourceIsReady(source) || source.state === 'unverified') { setConnect(vendor, undefined); return; }
		if (source.state === 'not_installed') { setConnect(vendor, { phase: 'failed', failed: 'install' }); return; }
		setConnect(vendor, undefined);
	}
	async function startInstall(source: ModelSource) {
		const current = deps.token();
		if (!current) return;
		starting.add(source.source);
		setConnect(source.source, { phase: 'installing' });
		try {
			const refusal = await startCliInstall(current, source.source);
			if (refusal === 'invisible_binary') await linkCli(current, source.source);
			if (refusal) { await afterInstalled(source.source); return; }
		} catch { setConnect(source.source, { phase: 'failed', failed: 'install' }); return; }
		finally { starting.delete(source.source); }
		await tickConnect();
	}
	/** Resolve a subscription choice once source detection can say whether an install is needed. */
	function sourcesChanged() {
		for (const [vendor, entry] of Object.entries(flow)) {
			if (entry?.phase !== 'awaiting-source') continue;
			const source = deps.sources().find((candidate) => candidate.source === vendor);
			if (!source || (source.state === 'unverified' && !source.checked_at)) continue;
			setConnect(vendor, undefined);
			if (source.state === 'not_installed') void install(source);
		}
	}
	function pick(vendor: string) {
		if (flow[vendor]) return;
		setConnect(vendor, { phase: 'awaiting-source' });
		sourcesChanged();
	}
	/** A tool picked on the subscriptions screen: install it now unless it is already there. */
	async function install(source: ModelSource) {
		const entry = flow[source.source];
		if (source.state !== 'not_installed' || entry?.phase === 'installing' || entry?.phase === 'signing-in') return;
		await startInstall(source);
	}
	/** A missing tool is installed first; signing in requires a separate action after installation. */
	async function signIn(source: ModelSource) {
		if (flow[source.source]?.phase === 'installing') return;
		if (source.state === 'not_installed') { await startInstall(source); return; }
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
		if (entry?.phase === 'awaiting-source') setConnect(vendor, undefined);
		else if (entry?.phase === 'installing') {
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
			if (Object.values(flow).some((entry) => entry?.phase === 'awaiting-source')) await deps.refreshSources(true);
			for (const [vendor, entry] of Object.entries(flow)) {
				if (entry?.phase === 'installing' && !starting.has(vendor)) {
					const install = await getCliInstall(current, vendor).catch(() => null);
					if (install?.state === 'done' || install?.state === 'idle') await afterInstalled(vendor, install.path_shadowed_by);
					else if (install?.state === 'cancelled') setConnect(vendor, undefined);
					else if (install?.state === 'failed') setConnect(vendor, { phase: 'failed', failed: 'install', reason: install.reason, cause: install.cause ?? undefined, ref: install.ref });
				} else if (entry?.phase === 'path-shadow') {
					const source = deps.sources().find((candidate) => candidate.source === vendor);
					if (source && sourceIsReady(source)) setConnect(vendor, undefined);
				} else if (entry?.phase === 'signing-in' && entry.accountId) {
					const signin = await checkSignin(current, entry.accountId).catch(() => undefined);
					if (signin?.status === 'completed' && signin.outcome == null) setConnect(vendor, { ...entry, checkingModels: true });
					else if (signin === null || signin?.status === 'completed' || (signin?.status === 'failed' && signin.cause === 'same_account' && signin.outcome === 'ready')) { await deps.refreshSources(); setConnect(vendor, undefined); }
					else if (signin && signin.status !== 'waiting') signinFailed(vendor, signin);
				}
			}
		} finally { ticking = false; }
	}
	function connectRow(source: ModelSource): RowView {
		const copy = VENDOR_COPY[source.source], entry = flow[source.source];
		const signInAction = { label: 'Sign in', run: () => void signIn(source) };
		if (entry?.phase === 'installing') {
			return { meta: `Installing ${copy.cli}…`, busy: true, spinning: true };
		}
		if (entry?.phase === 'signing-in' && entry.checkingModels) return { meta: `Checking your ${copy.account} models…`, busy: true, spinning: true };
		if (entry?.phase === 'signing-in') return { meta: 'Finish signing in in your browser.', busy: true, action: { label: 'Open sign-in again', run: () => void openSignInAgain(source) } };
		if (entry?.phase === 'failed' && entry.failed === 'signin') {
			const failure = signinFailureCopy(source.source, entry.signinCause, entry.ref);
			return { meta: failure.meta, reference: entry.ref ?? undefined, error: true, reportable: failure.reportable, action: signInAction, canTurnOff: entry.signinCause === 'signin_timed_out' };
		}
		if (entry?.phase === 'failed') {
			const failure = installFailureCopy(copy.cli, entry.cause, entry.ref, copy.maker);
			return { meta: failure.meta, reference: failure.reference, error: true, reportable: true, action: failure.retry ? { label: 'Retry', run: () => void startInstall(source) } : undefined };
		}
		if (entry?.phase === 'path-shadow') return { meta: `An older ${copy.cli} at ${entry.shadowedBinary} runs instead of the one CORSIA installed. Remove it, then come back here.`, error: true, action: { label: 'Check again', run: () => void deps.refreshSources(true) } };
		if (source.limited) return { meta: source.resets_at ? `Signed in, but your ${copy.account} subscription has reached its usage limit until ${formatMoment(new Date(source.resets_at))}.` : `Signed in, but your ${copy.account} subscription has reached its usage limit.`, error: true };
		if (sourceIsReady(source)) {
			const account = readyAccount(source);
			return { meta: account ? `Signed in as ${account}` : 'Signed in', ready: true };
		}
		if (hasNoUsableModel(source)) {
			return { meta: `This ${copy.account} account has no models for ${copy.cli}. Sign in with another account, or turn ${copy.cli} off.`, error: true, action: signInAction, canTurnOff: true };
		}
		if (source.accounts?.some((account) => account.state === 'unchecked')) {
			return { meta: `Couldn’t check your ${copy.account} models. Sign in again.`, error: true, action: signInAction, canTurnOff: true };
		}
		if (source.state === 'unverified' && source.cli_package_manager) {
			const command = source.cli_package_manager === 'homebrew' ? `brew reinstall ${source.source === 'claude-code' ? 'claude-code' : 'codex'}` : `npm install -g ${source.source === 'claude-code' ? '@anthropic-ai/claude-code' : '@openai/codex'}`;
			return { meta: `CORSIA can’t use this copy of ${copy.cli}. Reinstall it in Terminal with ${command}, then come back here.`, error: true, action: { label: 'Check again', run: () => void deps.refreshSources(true) } };
		}
		if (source.state === 'unverified') return { meta: source.reason ?? '', busy: !source.checked_at, action: source.checked_at ? { label: 'Check again', run: () => void deps.refreshSources(true) } : undefined };
		return { meta: copy.uses, action: signInAction };
	}
	return { get active() { return active; }, get attempts() { return attempts; }, tick: tickConnect, row: connectRow, install, pick, sourcesChanged, awaitingSource: (vendor: string) => flow[vendor]?.phase === 'awaiting-source', signIn, stop };
}
