import { sourceIsReady, type ModelSource } from '$lib/conductorSources';
import { accountToSignIn, cancelSignin, checkSignin, getCliInstall, linkCli, startCliInstall, startCliUpdate, startSignin } from '$lib/conductorSourcesApi';
import { openExternalUrl } from '$lib/linkTargetResolver';
import { fetchCorsiaAgent } from '$lib/utils/conductor';

// One vendor's place in install and sign-in; absent means the row shows its source's own state.
type ConnectPhase = { phase: 'confirm' | 'installing' | 'signing-in' | 'path-shadow' | 'failed'; operation?: 'install' | 'update'; installed?: boolean; accountId?: string; failed?: 'install' | 'signin'; reason?: string | null; shadowedBinary?: string };

export type RowView = {
	meta: string; ready?: boolean; busy?: boolean; error?: boolean; detail?: string;
	action?: { label: string; run: () => void };
	confirm?: { label: string; run: () => void }[];
	more?: { label: string; run: () => void }[];
};

export interface ConnectDeps {
	token: () => string | null;
	sources: () => ModelSource[];
	refreshSources: () => Promise<void>;
}

export const VENDOR_COPY: Record<string, { cli: string; uses: string; maker: string; origin: string; account: string; page: string }> = {
	'claude-code': { cli: 'Claude Code', uses: 'Uses your Claude subscription.', maker: 'Anthropic', origin: 'claude.ai', account: 'Claude', page: 'https://code.claude.com/docs/en/setup' },
	codex: { cli: 'Codex', uses: 'Uses your ChatGPT subscription.', maker: 'OpenAI', origin: 'chatgpt.com', account: 'ChatGPT', page: 'https://developers.openai.com/codex/cli' },
};

export const POLL_MS = 3000;

/** Install and sign-in for Claude Code and Codex inside setup, one row per vendor. */
export function createSetupConnect(deps: ConnectDeps) {
	let flow = $state<Record<string, ConnectPhase | undefined>>({});
	const active = $derived(Object.values(flow).some((entry) => entry?.phase === 'installing' || entry?.phase === 'signing-in'));

	function setConnect(vendor: string, next: ConnectPhase | undefined) {
		flow = { ...flow, [vendor]: next };
	}

	/** The workspace's CORSIA agent: the thread a sign-in binds its account to. */
	async function corsiaAgentThread(current: string): Promise<string> {
		const lookup = await fetchCorsiaAgent(current);
		if (lookup.kind !== 'target') throw new Error('CORSIA agent unavailable');
		return lookup.target.thread_id;
	}

	/** Start the vendor's browser sign-in in place; the poll below sees it complete. */
	async function beginSignIn(source: ModelSource, installed = false) {
		const current = deps.token();
		if (!current) return;
		const vendor = source.source;
		setConnect(vendor, { phase: 'signing-in', installed });
		try {
			const target = await corsiaAgentThread(current);
			const accountId = await accountToSignIn(current, vendor, source.name);
			const result = await startSignin(current, accountId, target);
			if (result.status === 'failed' || result.status === 'unavailable' || result.status === 'refused') throw new Error(`signin ${result.status}`);
			setConnect(vendor, { phase: 'signing-in', installed, accountId });
			await tickConnect();
		} catch {
			setConnect(vendor, { phase: 'failed', failed: 'signin' });
		}
	}

	async function openSignInAgain(source: ModelSource) {
		const current = deps.token();
		const accountId = flow[source.source]?.accountId;
		if (current && accountId) await cancelSignin(current, accountId).catch(() => undefined);
		await beginSignIn(source, flow[source.source]?.installed);
	}

	/** Refresh the source after an installer run, preserving a PATH shadow instead of hiding it as Ready. */
	async function afterInstalled(vendor: string, shadowedBinary: string | null = null) {
		await deps.refreshSources();
		const source = deps.sources().find((candidate) => candidate.source === vendor);
		if (shadowedBinary) { setConnect(vendor, { phase: 'path-shadow', shadowedBinary }); return; }
		if (!source || sourceIsReady(source) || source.state === 'unverified') { setConnect(vendor, undefined); return; }
		if (source.state === 'not_installed') { setConnect(vendor, { phase: 'failed', failed: 'install', reason: null }); return; }
		await beginSignIn(source, true);
	}

	async function startInstall(source: ModelSource, operation: 'install' | 'update' = 'install') {
		const current = deps.token();
		if (!current) return;
		const vendor = source.source;
		setConnect(vendor, { phase: 'installing', operation });
		try {
			const refusal = operation === 'update' ? await startCliUpdate(current, vendor) : await startCliInstall(current, vendor);
			if (refusal === 'invisible_binary') await linkCli(current, vendor);
			if (refusal) { await afterInstalled(vendor); return; }
			await tickConnect();
		} catch {
			setConnect(vendor, { phase: 'failed', failed: 'install', operation, reason: null });
		}
	}

	let ticking = false;
	/** One look at every install and sign-in under way. */
	async function tickConnect() {
		const current = deps.token();
		if (!current || ticking) return;
		ticking = true;
		try {
			for (const [vendor, entry] of Object.entries(flow)) {
				if (entry?.phase === 'installing') {
					const install = await getCliInstall(current, vendor).catch(() => null);
					if (install?.state === 'done' || install?.state === 'idle') await afterInstalled(vendor, install.path_shadowed_by);
					else if (install?.state === 'failed') setConnect(vendor, { phase: 'failed', failed: 'install', operation: entry.operation, reason: install.reason });
				} else if (entry?.phase === 'path-shadow') {
					const source = deps.sources().find((candidate) => candidate.source === vendor);
					if (!source || sourceIsReady(source)) setConnect(vendor, undefined);
				} else if (entry?.phase === 'signing-in' && entry.accountId) {
					const signin = await checkSignin(current, entry.accountId).catch(() => null);
					if (signin?.status === 'completed') { await deps.refreshSources(); setConnect(vendor, undefined); }
					else if (signin && signin.status !== 'waiting') setConnect(vendor, { phase: 'failed', failed: 'signin' });
				}
			}
		} finally { ticking = false; }
	}

	function connectRow(source: ModelSource): RowView {
		const copy = VENDOR_COPY[source.source] ?? { cli: source.name, uses: '', maker: source.name, origin: '', account: source.name, page: '' };
		const entry = flow[source.source];
		const lines = (rest: string) => `${copy.uses} ${rest}`.trim();
		if (entry?.phase === 'path-shadow') {
			return {
				meta: lines(`CORSIA installed the current ${copy.cli}, but an older copy comes first on this computer’s PATH, so CORSIA still runs that one.`),
				detail: `Remove the older copy at ${entry.shadowedBinary}, then come back to this window.`, error: true,
			};
		}
		if (sourceIsReady(source)) return { meta: copy.uses, ready: true };
		if (entry?.phase === 'confirm') {
			return {
				meta: lines(`CORSIA installs ${copy.cli} for you.`),
				detail: `${entry.operation === 'update' ? 'Update' : 'Set up'} ${copy.cli}? This runs ${copy.maker}’s official installer, downloaded from ${copy.origin}, and then you sign in to your ${copy.account} account.`,
				confirm: [{ label: entry.operation === 'update' ? 'Update' : 'Set up', run: () => void startInstall(source, entry.operation) }, { label: 'Cancel', run: () => setConnect(source.source, undefined) }],
			};
		}
		if (entry?.phase === 'installing') return { meta: `${entry.operation === 'update' ? 'Updating' : 'Installing'} with ${copy.maker}’s official installer. This can take up to 3 minutes.`, busy: true };
		if (entry?.phase === 'signing-in') {
			return {
				meta: `${entry.installed ? 'Installed. ' : ''}Finish signing in to your ${copy.account} account in the browser window that just opened.`,
				busy: true, action: { label: 'Open sign-in again', run: () => void openSignInAgain(source) },
			};
		}
		if (entry?.phase === 'failed' && entry.failed === 'signin') {
			return { meta: lines(`${copy.cli} is installed.`), error: true, detail: `Couldn’t sign in to ${copy.cli}. Choose Sign in to try again.`, action: { label: 'Sign in', run: () => void beginSignIn(source) } };
		}
		if (entry?.phase === 'failed') {
			const why = entry.reason ? `: ${entry.reason}` : '';
			const next = entry.reason?.includes('internet') ? 'Check your connection, then choose Try again.' : 'Choose Try again.';
			return {
				meta: lines(''), error: true, detail: `Couldn’t ${entry.operation === 'update' ? 'update' : 'install'} ${copy.cli}${why}. ${next}`,
				action: { label: 'Try again', run: () => void startInstall(source) },
				more: copy.page ? [{ label: 'Open the install page', run: () => void openExternalUrl(copy.page) }] : undefined,
			};
		}
		if (source.state === 'not_installed') return { meta: lines(`CORSIA installs ${copy.cli} for you.`), action: { label: 'Set up', run: () => setConnect(source.source, { phase: 'confirm' }) } };
		if (source.limited) return { meta: lines('It has reached its usage limit for now.') };
		if (source.state === 'unverified' && source.cli_package_manager) {
			const command = source.cli_package_manager === 'homebrew'
				? `brew upgrade ${source.source === 'claude-code' ? 'claude-code' : 'codex'}`
				: `npm update -g ${source.source === 'claude-code' ? '@anthropic-ai/claude-code' : '@openai/codex'}`;
			const manager = source.cli_package_manager === 'homebrew' ? 'Homebrew' : 'npm';
			return { meta: lines(`CORSIA can’t use this copy of ${copy.cli}, which ${manager} installed.`), detail: `Update it in Terminal with ${command}. CORSIA notices when you come back to this window.`, error: true };
		}
		if (source.state === 'unverified') return { meta: lines(`CORSIA can’t use this version of ${copy.cli}. Update installs the current one with ${copy.maker}’s official installer.`), action: { label: 'Update', run: () => setConnect(source.source, { phase: 'confirm', operation: 'update' }) } };
		return { meta: lines(`${copy.cli} is installed. Sign in to use it.`), action: { label: 'Sign in', run: () => void beginSignIn(source) } };
	}

	return {
		get active() { return active; },
		tick: tickConnect,
		row: connectRow,
	};
}
