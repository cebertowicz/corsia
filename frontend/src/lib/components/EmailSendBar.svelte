<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import { discardEmailDraft, sendEmailDraft, type DraftActionResult } from '$lib/api/emailDrafts';
	import type { ConversationFailure, EmailDraftState } from '$lib/emailDraft';

	let { artifactId, text, readAt = null, initialState = 'open', readOnly = false, connected = true, blocker = null, ended = $bindable(false), onReread = () => {}, onRefused = () => {} }: {
		artifactId: string; text: () => string; readAt?: string | null; initialState?: EmailDraftState;
		readOnly?: boolean; connected?: boolean; blocker?: string | null; ended?: boolean;
		onReread?: () => void; onRefused?: (reason: ConversationFailure) => void;
	} = $props();
	type Step = 'draft' | 'confirm' | 'sending' | 'sent' | 'failed' | 'declined' | 'unconfirmed';
	let step = $state<Step>(untrack(() => initialState === 'sent' ? 'sent' : initialState === 'discarded' ? 'declined' : initialState === 'sending' ? 'unconfirmed' : 'draft'));
	let message = $state('');
	let notDeliveredTo = $state<string[]>([]);
	const reasonId = $props.id();
	const canSend = $derived(connected && !blocker && !readOnly);
	$effect(() => { ended = step === 'sent' || step === 'declined'; });
	onDestroy(() => { ended = false; });

	function apply(result: DraftActionResult) {
		switch (result.state) {
			case 'sent': notDeliveredTo = result.not_delivered_to ?? []; step = 'sent'; break;
			case 'discarded': step = 'declined'; break;
			case 'sending': case 'unconfirmed': step = 'unconfirmed'; break;
			case 'failed': step = 'failed'; break;
			case 'changed': case 'newer':
				message = result.message; step = 'draft';
				if (result.state === 'newer') onReread();
				break;
			case 'signin': case 'missing':
				step = 'draft'; onRefused(result.state); break;
		}
	}
	async function startSend() {
		if (!canSend || (step !== 'draft' && step !== 'failed')) return;
		step = 'sending'; message = '';
		try { apply(await sendEmailDraft(artifactId, text(), readAt)); } catch { step = 'failed'; }
	}
	async function decline() {
		if (step !== 'confirm' || !connected || readOnly) return;
		step = 'sending';
		try { apply(await discardEmailDraft(artifactId)); } catch { step = 'failed'; }
	}
</script>

<div class="email-send-bar" role="group" aria-label="Send this email">
	{#if readOnly && step === 'draft'}
		<span class="bar-text" role="status">Read-only. This draft can’t be sent from here.</span>
	{:else if step === 'draft'}
		{#if message}<span class="bar-text" role="status">{message}</span>{/if}
		{#if !connected}
			<span class="bar-text" id={reasonId} role="status">Offline. Send is available when you reconnect.</span>
		{:else if blocker}
			<span class="bar-text" id={reasonId} role="status">{blocker} is unavailable. Remove it to send.</span>
		{/if}
		<span class="bar-actions">
			<Button size="sm" variant="secondary" disabled={!connected} onclick={() => (step = 'confirm')}>Don’t send</Button>
			<Button size="sm" variant="primary" ariaDisabled={!canSend} describedby={canSend ? undefined : reasonId} onclick={startSend}>Send</Button>
		</span>
	{:else if step === 'confirm'}
		<span class="bar-text" role="status">Don’t send this email?</span>
		<span class="bar-actions"><Button size="sm" variant="ghost" onclick={() => (step = 'draft')}>Keep draft</Button><Button size="sm" variant="secondary" onclick={decline}>Don’t send</Button></span>
	{:else if step === 'sending'}
		<span class="bar-text" role="status">Sending…</span>
	{:else if step === 'sent'}
		<span class="bar-text" role="status">Sent{notDeliveredTo.length ? `. ${notDeliveredTo.join(', ')} didn't get it.` : ''}</span>
	{:else if step === 'failed'}
		<span class="bar-text failed" role="alert">Couldn’t send. The email didn’t leave, and the draft is kept.</span>
		<span class="bar-actions"><Button size="sm" variant="secondary" disabled={!connected || readOnly} onclick={() => (step = 'confirm')}>Don’t send</Button><Button size="sm" variant="primary" ariaDisabled={!canSend} describedby={canSend ? undefined : reasonId} onclick={startSend}>Retry</Button></span>
	{:else if step === 'unconfirmed'}
		<span class="bar-text" role="status">Couldn’t confirm the email left. Check your Sent mail before sending again.</span>
	{:else}
		<span class="bar-text" role="status">Not sent. This draft is marked as not to send.</span>
	{/if}
</div>

<style>
	.email-send-bar { flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: var(--space-1) var(--space-2); min-height: var(--space-7); padding: var(--space-1) var(--doc-phone-inset, var(--space-3)) var(--space-1) var(--doc-phone-inset, var(--doc-rail, 44px)); border-bottom: 1px solid var(--border-subtle); }
	.bar-text { flex: 1 1 12rem; min-width: 0; font-size: var(--text-footnote); line-height: var(--lh-normal); color: var(--text-secondary); overflow-wrap: anywhere; }
	.bar-text.failed { color: var(--red-text); }
	.bar-actions { flex: 0 0 auto; display: inline-flex; gap: var(--space-2); }
	.email-send-bar :global(.btn) { flex: 0 0 auto; }
	@media (pointer: coarse) { .email-send-bar { min-height: var(--touch-target); } }
</style>
