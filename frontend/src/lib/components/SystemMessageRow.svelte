<script module lang="ts">
	let requestedAccounts = false;
	/** Accounts whose sign-in the rows have already checked once: a long chat sends one check per account, not one per row. */
	const checkedAccounts = new Set<string>();
	/** Rows whose account state was read live when they appeared: the page's list can predate the sign-out the row reports. */
	const liveReadRows = new Map<string, Promise<unknown>>();
	let accountsRead: Promise<unknown> | null = null;
	/** Rows that mount together share one read. */
	function readAccountsOnce(load: () => Promise<unknown>) {
		accountsRead ??= load().finally(() => { accountsRead = null; });
		return accountsRead;
	}
</script>

<script lang="ts">
	/**
	 * One system row in the transcript, and the styling of every kind of it.
	 *
	 * Extracted from `MessageList.svelte` so the list holds the shape of the
	 * transcript — which zone a message belongs to, and in what order — and this
	 * holds what a system notice looks like. The list was over its size budget
	 * before this split and the two concerns were interleaved.
	 */
	import { loadVendorAccounts, vendorAccounts, signin, rememberVisibleSignin, startVendorSignin, checkVendorSignin, cancelVendorSignin, refreshAndRecordModels } from '$lib/stores/vendorAccounts';
	import { signinMessage } from './v7/accountSigninMessage';
	import { isHeldSignedOutNotice } from './heldAccountNotice';
	import { VENDORS } from '$lib/conductorSources';
	import SubagentChip from './SubagentChip.svelte';
	import EventChip from './EventChip.svelte';
	import SendRefChip from './SendRefChip.svelte';
	import AccountSwitchReceipt from './AccountSwitchReceipt.svelte';
	import {
		artifactRemovalEventLabel,
		isActivityReceipt,
		isArtifactRemovalEvent,
		isPassiveContextEvent,
		passiveContextEventLabel,
	} from './messageListModel';

	let { msg }: {
		msg: any;
	} = $props();

	const passiveContextEvent = $derived(isPassiveContextEvent(msg));
	const activityReceipt = $derived(isActivityReceipt(msg));
	const artifactRemovalEvent = $derived(isArtifactRemovalEvent(msg));
	const subagentDone = $derived(msg.role === 'system' && msg.metadata?.type === 'subagent_done');
	const signInExpired = $derived(msg.metadata?.cause === 'sign_in_expired');
	/** Existing error text identifies the account recovery journey; unrelated account links still navigate to Settings. */
	const PROVIDERS_LINK_TEXT = 'Settings › Providers';
	const accountId = $derived(typeof msg.metadata?.account_id === 'string' && /^[\w-]+$/.test(msg.metadata.account_id) ? msg.metadata.account_id : null);
	const namedAccount = $derived(accountId ? $vendorAccounts.find((account) => account.id === accountId) : undefined);
	const linkableAccount = $derived(!!namedAccount && namedAccount.state !== 'removed');
	const content = $derived(String(msg.content ?? ''));
	const linkAt = $derived(linkableAccount ? content.indexOf(PROVIDERS_LINK_TEXT) : -1);
	const staleModels = $derived(msg.metadata?.cause === 'account_model_cache_changed');
	/** A turn that ended on the exact expired-login error carries `cause_kind` (`llm/error_cause.py`), and its row names the account. */
	const expiredLogin = $derived(signInExpired || msg.metadata?.cause_kind === 'sign_in_expired');
	const activeSignin = $derived(accountId ? $signin[accountId] : undefined);
	/** A row offers live recovery only while the account is in that state now; otherwise it reads as history. */
	const accountRecovery = $derived(linkableAccount && (staleModels || (expiredLogin && (namedAccount?.state === 'signed_out' || !!activeSignin))));
	/** The page's list can predate a sign-out, so a row with no sign-in under way says "signed in now" only after it has read the account itself. */
	let liveRead = $state(false);
	/** An expired-login row whose account is signed in again, with no sign-in under way, reads as history from the account's state (live, after Check, and after a reload). A held message's row is not history: it goes when its message sends. */
	const expiredHistory = $derived(expiredLogin && namedAccount?.state === 'signed_in' && (activeSignin?.status === 'completed' || (!activeSignin && liveRead)) && !isHeldSignedOutNotice(msg));
	let busy = $state(false);
	let refreshing = $state(false);
	let actionError = $state('');
	function nameOf(id: string | null | undefined) { return $vendorAccounts.find((a) => a.id === id)?.label.text ?? null; }
	function chatStatus(text: string) { return /[.!?]$/.test(text) ? text : `${text}.`; }
	async function accountAction(action: 'start' | 'check' | 'cancel') {
		if (!accountId || busy) return;
		busy = true; actionError = '';
		const result = await (action === 'start' ? startVendorSignin(accountId) : action === 'check' ? checkVendorSignin(accountId) : cancelVendorSignin(accountId));
		busy = false;
		if (result.signin) signin.update((current) => rememberVisibleSignin(current, result.signin!, $vendorAccounts));
		if (!result.ok && !('absent' in result && result.absent)) actionError = result.error ?? 'Could not check sign-in.';
		else if (result.error) actionError = result.error;
	}
	async function refreshModels() {
		if (!accountId || busy) return;
		busy = true; refreshing = true; actionError = '';
		const result = await refreshAndRecordModels(accountId);
		busy = false; refreshing = false;
		if (result.signedOut && result.error) actionError = result.error;
	}
	$effect(() => {
		if (accountRecovery && accountId && !checkedAccounts.has(accountId)) {
			checkedAccounts.add(accountId);
			if (!$signin[accountId]) void accountAction('check');
		}
	});
	$effect(() => {
		if (accountId && $vendorAccounts.length === 0 && !requestedAccounts) {
			requestedAccounts = true;
			void loadVendorAccounts();
		}
	});
	// An expired-login row decides between live recovery and history on the account's state now, so it reads that once.
	$effect(() => {
		if (!(accountId && expiredLogin && namedAccount && namedAccount.state !== 'signed_out' && !activeSignin)) return;
		const key = String(msg.id ?? `${accountId}:${content}`);
		let read = liveReadRows.get(key);
		if (!read) { read = readAccountsOnce(() => loadVendorAccounts()); liveReadRows.set(key, read); }
		const done = () => { liveRead = true; };
		void read.then(done, done);
	});
	const quietRow = $derived(
		passiveContextEvent || activityReceipt || artifactRemovalEvent || subagentDone,
	);

	/** The single line a quiet context row shows: the row's own content, never new copy. */
	function quietLabel(): string {
		if (artifactRemovalEvent) return artifactRemovalEventLabel(msg);
		if (passiveContextEvent) return passiveContextEventLabel(msg);
		return String(msg.content ?? '');
	}
</script>

{#if msg.metadata?.type === 'subagent_ref'}
	<SubagentChip {msg} />
{:else if ['context_event', 'activity_receipt'].includes(msg.metadata?.type)}
	<EventChip {msg} />
{:else if msg.metadata?.type === 'send_ref'}
	<SendRefChip {msg} />
{:else if msg.metadata?.type === 'account_switch'}
	<AccountSwitchReceipt {msg} />
{:else}
	<div class="system-event-wrap" class:context-event-row={quietRow}>
		{#if artifactRemovalEvent || passiveContextEvent || subagentDone}
			<div class="message-content context-event">
				<span class="context-event-label">{quietLabel()}</span>
			</div>
		{:else}
			<div
				class="message-content"
				class:model-switch={msg.metadata?.type === 'model_switch'}
				class:system-interrupt={msg.metadata?.type === 'system_interrupt'}
				class:sandbox-denial={msg.metadata?.type === 'sandbox_denial'}
				class:harness-error={!accountRecovery && !expiredHistory && (msg.metadata?.event_type === 'harness.error' || msg.metadata?.type === 'harness_error')}
				class:account-recovery={accountRecovery || expiredHistory}
				class:recovery-working={expiredHistory || (accountRecovery && (activeSignin?.status === 'waiting' || activeSignin?.status === 'already_signed_in'))}
				class:recovery-failure={accountRecovery && !!activeSignin && ['failed', 'unavailable', 'model_refresh_failed'].includes(activeSignin.status)}
			>
				{#if expiredHistory && namedAccount}
					<span>{namedAccount.label.text} was signed out, so this message got no answer. It’s signed in now.</span>
				{:else if accountRecovery && namedAccount}
					{#if !(activeSignin?.status === 'completed' && namedAccount.state === 'signed_in')}
						{#if activeSignin}
							<span>{chatStatus(staleModels && activeSignin.status === 'model_refresh_failed' ? `Couldn’t refresh the model list.${activeSignin.ref ? ` Reference: ${activeSignin.ref}` : ''}` : signinMessage(activeSignin, nameOf, $signin, VENDORS[namedAccount.provider]?.cli ?? null))}</span>
							{#if activeSignin.status === 'waiting'}
								<span class="recovery-actions">
									<button disabled={busy} onclick={() => void accountAction('check')}>Check</button>
									<button disabled={busy} onclick={() => void accountAction('cancel')}>Cancel</button>
								</span>
							{:else if activeSignin.status === 'model_refresh_failed'}
								<button disabled={busy} onclick={() => void refreshModels()}>{refreshing ? 'Refreshing…' : 'Refresh models'}</button>
							{:else if activeSignin.status === 'refused'}
								<button disabled={busy} onclick={() => void accountAction('start')}>Sign in</button>
							{:else if ['failed', 'cancelled', 'unavailable', 'superseded'].includes(activeSignin.status)}
								<button disabled={busy} onclick={() => void accountAction('start')}>Sign in again</button>
							{/if}
						{:else if staleModels && namedAccount.state !== 'signed_out'}
							<span>Couldn’t start {VENDORS[namedAccount.provider]?.cli ?? 'the tool'} on {namedAccount.label.text}: its model list is out of date.</span>
							<button disabled={busy} onclick={() => void refreshModels()}>{refreshing ? 'Refreshing…' : 'Refresh models'}</button>
						{:else}
							<span>{namedAccount.label.text} is signed out of {VENDORS[namedAccount.provider]?.cli ?? 'this tool'}.</span>
							<button disabled={busy} onclick={() => void accountAction('start')}>Sign in</button>
						{/if}
						{#if actionError}<span>{actionError}</span>{/if}
					{/if}
				{:else if linkAt >= 0}
					<span class="md-content">{content.slice(0, linkAt)}<a href={`/settings/providers#account-${accountId}`} target="_blank" rel="noopener">{PROVIDERS_LINK_TEXT}</a>{content.slice(linkAt + PROVIDERS_LINK_TEXT.length)}</span>
				{:else if signInExpired}
					<p class="recovery-line">Claude's sign-in has expired. Sign in again from the model card.</p>
					<details class="failure-details"><summary>Show details</summary><div>{msg.content}</div></details>
				{:else}
					{msg.content}
				{/if}
			</div>
		{/if}
	</div>
{/if}

<style>
	.system-event-wrap {
		display: flex;
		align-items: flex-start;
		justify-content: center;
		gap: 6px;
	}
	.system-event-wrap.context-event-row {
		align-items: center;
	}
	.message-content {
		font-size: var(--text-subhead); color: var(--amber-text); text-align: center;
		padding: 8px; background: var(--amber-bg);
		border-radius: var(--radius-sm);
	}
	.message-content.account-recovery { min-width: 0; overflow-wrap: anywhere; color: var(--amber-text); background: transparent; padding: var(--space-1) 0; }
	.message-content.account-recovery.recovery-working { color: var(--text-secondary); }
	.message-content.account-recovery.recovery-failure { color: var(--red-text); }
	/* The reduced-motion rule gives every element a 0.01ms transition, which lags a recolour behind its new text by a frame. */
	.message-content.account-recovery, .account-recovery button { transition-property: none; }
	.recovery-actions { display: inline-flex; flex-wrap: wrap; gap: var(--space-5); }
	.account-recovery button { position: relative; border: 0; background: transparent; color: var(--accent-text); font: inherit; white-space: nowrap; cursor: pointer; }
	@media (pointer: coarse) {
		/* Keep expanded touch targets inside the transcript row's clipping bounds. */
		.message-content.account-recovery { padding-block: var(--space-3); }
		.account-recovery button::after { content: ''; position: absolute; left: 50%; top: 50%; width: max(var(--touch-target), calc(100% + var(--space-4))); height: max(var(--touch-target), calc(100% + var(--space-4))); transform: translate(-50%, -50%); }
	}
	.account-recovery button:disabled { color: var(--text-secondary); cursor: wait; }
	.account-recovery button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.recovery-line { margin: 0; }
	.failure-details { margin-top: 6px; text-align: left; }
	.failure-details summary { cursor: pointer; }
	.failure-details > div { margin-top: 6px; white-space: pre-wrap; }
	.message-content.model-switch {
		font-size: var(--text-footnote); color: var(--text-tertiary);
		background: none; padding: 4px 0;
		border-top: 1px solid var(--border-subtle);
		border-bottom: 1px solid var(--border-subtle);
		border-radius: 0;
	}
	.message-content.system-interrupt {
		color: var(--red);
		background: rgba(239, 68, 68, 0.08);
		border: 1px solid rgba(239, 68, 68, 0.2);
	}
	.message-content.sandbox-denial {
		color: var(--orange, #f97316);
		background: rgba(249, 115, 22, 0.08);
		border: 1px solid rgba(249, 115, 22, 0.2);
		text-align: left;
		font-family: var(--font-mono);
		font-size: var(--text-footnote);
	}
	.message-content.harness-error {
		color: var(--red);
		background: rgba(239, 68, 68, 0.08);
		border: 1px solid rgba(239, 68, 68, 0.22);
		text-align: left;
		white-space: pre-wrap;
	}
	.message-content.context-event {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 5px;
		max-width: min(360px, calc(100vw - 88px));
		color: var(--text-tertiary);
		background: transparent;
		border: 0;
		font-size: var(--text-footnote);
		font-family: inherit;
		line-height: 1.25;
		padding: 2px 4px;
	}
	.context-event-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
