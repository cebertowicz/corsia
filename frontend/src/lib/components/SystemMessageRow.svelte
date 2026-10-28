<script module lang="ts">
	let requestedAccounts = false;
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
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';
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
	/** A lost-login error names the account to sign in again; "Settings › Providers" in its text then links to that account's row. Default and legacy accounts have no row to press, so their errors stay text. */
	const PROVIDERS_LINK_TEXT = 'Settings › Providers';
	const accountId = $derived(typeof msg.metadata?.account_id === 'string' && /^[\w-]+$/.test(msg.metadata.account_id) ? msg.metadata.account_id : null);
	const namedAccount = $derived(accountId ? $vendorAccounts.find((account) => account.id === accountId) : undefined);
	const linkableAccount = $derived(!!namedAccount && !namedAccount.is_default && namedAccount.state !== 'legacy' && namedAccount.state !== 'removed');
	const content = $derived(String(msg.content ?? ''));
	const linkAt = $derived(linkableAccount ? content.indexOf(PROVIDERS_LINK_TEXT) : -1);
	$effect(() => {
		if (accountId && $vendorAccounts.length === 0 && !requestedAccounts) {
			requestedAccounts = true;
			void loadVendorAccounts();
		}
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
				class:harness-error={msg.metadata?.event_type === 'harness.error' || msg.metadata?.type === 'harness_error'}
			>
				{#if linkAt >= 0}
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
