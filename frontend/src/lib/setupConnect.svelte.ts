import { sourceIsReady, type ModelSource } from '$lib/conductorSources';
import { accountToSignIn, cancelCliInstall, cancelSignin, checkSignin, getCliInstall, linkCli, startCliInstall, startSignin } from '$lib/conductorSourcesApi';
import { fetchCorsiaAgent } from '$lib/utils/conductor';
import { formatMoment } from '$lib/time';

type InstallFailureCause = 'connection' | 'site_refused' | 'download_unverified' | 'installer_failed';
type ConnectPhase = { phase: 'installing' | 'signing-in' | 'path-shadow' | 'failed'; installed?: boolean; accountId?: string; failed?: 'install' | 'signin'; reason?: string | null; cause?: InstallFailureCause; ref?: string | null; shadowedBinary?: string };
export type SetupAttempt = { attempt: 'none' | 'installed' | 'install_failed' | 'signin_failed'; cause?: string; ref?: string };
export type RowView = {
	meta: string; ready?: boolean; busy?: boolean; spinning?: boolean; error?: boolean; detail?: string; reportable?: boolean;
	action?: { label: string; run: () => void };
	confirm?: { label: string; run: () => void }[];
	more?: { label: string; run: () => void }[];
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

/** Keep refusal recovery distinct from retryable installer failures without exposing installer output. */
export function installFailureCopy(cli: string, cause?: InstallFailureCause | null, ref?: string | null) {
	const reference = ref ? ` Reference: ${ref}` : '';
	if (installCause(cause) === 'site_refused') {
		return { meta: `Couldn’t download ${cli}. The download site refused the request. Save a report.${reference}`, retry: false };
	}
	if (installCause(cause) === 'connection') {
		return { meta: `Couldn’t download ${cli}. Check your internet connection, then retry.`, retry: true };
	}
	return { meta: `Couldn’t install ${cli}. Retry, or save a report.${reference}`, retry: true };
}

/** Install once on Set up, then sign in; retain setup-only attempts for the handoff. */
export function createSetupConnect(deps: ConnectDeps) {
	let flow = $state<Record<string, ConnectPhase | undefined>>({});
	let attempts = $state<Record<string, SetupAttempt>>({ 'claude-code': { attempt: 'none' }, codex: { attempt: 'none' } });
	const active = $derived(Object.values(flow).some((entry) => entry?.phase === 'installing' || entry?.phase === 'signing-in'));
	function setConnect(vendor: string, next: ConnectPhase | undefined) {
		flow = { ...flow, [vendor]: next };
		if (next?.phase === 'failed') attempts = { ...attempts, [vendor]: { attempt: next.failed === 'signin' ? 'signin_failed' : 'install_failed', cause: next.failed === 'signin' ? 'signin_failed' : installCause(next.cause), ref: next.ref ?? undefined } };
	}
	async function beginSignIn(source: ModelSource, installed = false) {
		const current = deps.token();
		if (!current) return;
		setConnect(source.source, { phase: 'signing-in', installed });
		try {
			const lookup = await fetchCorsiaAgent(current);
			if (lookup.kind !== 'target') throw new Error('CORSIA agent unavailable');
			const accountId = await accountToSignIn(current, source.source, source.name);
			const result = await startSignin(current, accountId, lookup.target.thread_id);
			if (['failed', 'unavailable', 'refused'].includes(result.status)) throw new Error('signin failed');
			setConnect(source.source, { phase: 'signing-in', installed, accountId });
			await tickConnect();
		} catch { setConnect(source.source, { phase: 'failed', failed: 'signin' }); }
	}
	async function openSignInAgain(source: ModelSource) {
		const current = deps.token(), accountId = flow[source.source]?.accountId;
		if (current && accountId) await cancelSignin(current, accountId).catch(() => undefined);
		await beginSignIn(source, flow[source.source]?.installed);
	}
	async function afterInstalled(vendor: string, shadowedBinary: string | null = null) {
		attempts = { ...attempts, [vendor]: { attempt: 'installed' } };
		await deps.refreshSources();
		const source = deps.sources().find((candidate) => candidate.source === vendor);
		if (shadowedBinary) { setConnect(vendor, { phase: 'path-shadow', shadowedBinary }); return; }
		if (!source || sourceIsReady(source) || source.state === 'unverified') { setConnect(vendor, undefined); return; }
		if (source.state === 'not_installed') { setConnect(vendor, { phase: 'failed', failed: 'install' }); return; }
		await beginSignIn(source, true);
	}
	async function startInstall(source: ModelSource) {
		const current = deps.token();
		if (!current) return;
		setConnect(source.source, { phase: 'installing' });
		try {
			const refusal = await startCliInstall(current, source.source);
			if (refusal === 'invisible_binary') await linkCli(current, source.source);
			if (refusal) { await afterInstalled(source.source); return; }
			await tickConnect();
		} catch { setConnect(source.source, { phase: 'failed', failed: 'install' }); }
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
					if (signin?.status === 'completed') { await deps.refreshSources(); setConnect(vendor, undefined); }
					else if (signin && signin.status !== 'waiting') setConnect(vendor, { phase: 'failed', failed: 'signin' });
				}
			}
		} finally { ticking = false; }
	}
	function connectRow(source: ModelSource): RowView {
		const copy = VENDOR_COPY[source.source], entry = flow[source.source];
		if (entry?.phase === 'installing') return { meta: `Installing ${copy.cli}…`, busy: true, spinning: true, action: { label: 'Cancel', run: () => void cancelInstall(source) } };
		if (entry?.phase === 'signing-in') return { meta: 'Finish signing in in your browser.', busy: true, action: { label: 'Open sign-in again', run: () => void openSignInAgain(source) } };
		if (entry?.phase === 'failed' && entry.failed === 'signin') return { meta: 'Couldn’t finish signing in. Sign in again.', error: true, action: { label: 'Sign in', run: () => void beginSignIn(source) } };
		if (entry?.phase === 'failed') {
			const failure = installFailureCopy(copy.cli, entry.cause, entry.ref);
			return { meta: failure.meta, error: true, reportable: true, action: failure.retry ? { label: 'Retry', run: () => void startInstall(source) } : undefined };
		}
		if (entry?.phase === 'path-shadow') return { meta: `An older ${copy.cli} at ${entry.shadowedBinary} runs instead of the one CORSIA installed. Remove it, then come back here.`, error: true, action: { label: 'Check again', run: () => void deps.refreshSources(true) } };
		if (source.limited) return { meta: source.resets_at ? `Signed in, but your ${copy.account} subscription has reached its usage limit until ${formatMoment(new Date(source.resets_at))}.` : `Signed in, but your ${copy.account} subscription has reached its usage limit.` };
		if (sourceIsReady(source)) return { meta: `Signed in with your ${copy.account} account.`, ready: true };
		if (source.state === 'not_installed') return { meta: `For ${copy.account} subscribers. CORSIA installs it from ${copy.maker}, then you sign in.`, action: { label: 'Set up', run: () => void startInstall(source) } };
		if (source.state === 'unverified' && source.cli_package_manager) {
			const command = source.cli_package_manager === 'homebrew' ? `brew reinstall ${source.source === 'claude-code' ? 'claude-code' : 'codex'}` : `npm install -g ${source.source === 'claude-code' ? '@anthropic-ai/claude-code' : '@openai/codex'}`;
			return { meta: `CORSIA can’t use this copy of ${copy.cli}. Reinstall it in Terminal with ${command}, then come back here.`, error: true, action: { label: 'Check again', run: () => void deps.refreshSources(true) } };
		}
		if (source.state === 'unverified') return { meta: source.reason ?? '', busy: !source.checked_at, action: source.checked_at ? { label: 'Check again', run: () => void deps.refreshSources(true) } : undefined };
		return { meta: `Installed. Sign in with your ${copy.account} subscription.`, action: { label: 'Sign in', run: () => void beginSignIn(source) } };
	}
	return { get active() { return active; }, get attempts() { return attempts; }, tick: tickConnect, row: connectRow };
}
