<script lang="ts">
	import { getContext, onDestroy, untrack } from 'svelte';
	import { ASKER_CHAT, ASKER_WHO } from './ReviewBarShell.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import { discardEmailDraft, sendEmailDraft, type DraftActionResult } from '$lib/api/emailDrafts';
	import type { ConversationFailure, EmailDraftState } from '$lib/emailDraft';

	let { artifactId, text, targetVersion = null, readAt = null, initialState = 'open', readOnly = false, connected = true, blocker = null, ended = $bindable(false), flush = async () => true, readState = async () => null, onReread = () => {}, onRefused = () => {} }: {
		artifactId: string; text: () => string; targetVersion?: string | null; readAt?: string | null; initialState?: EmailDraftState;
		readOnly?: boolean; connected?: boolean; blocker?: string | null; ended?: boolean;
		/** Saves every pending edit; false when the save failed. Send waits on it, so the text it carries is the saved text. */
		flush?: () => Promise<boolean>;
		/** The draft's state as the server holds it, or null when it can't be read. */
		readState?: () => Promise<EmailDraftState | null>;
		onReread?: () => void; onRefused?: (reason: ConversationFailure) => void;
	} = $props();
	type Step = 'draft' | 'confirm' | 'sending' | 'sent' | 'failed' | 'declined' | 'unconfirmed';
	const fromState = (state: EmailDraftState): Step => state === 'sent' ? 'sent' : state === 'discarded' ? 'declined' : state === 'sending' ? 'unconfirmed' : 'draft';
	let step = $state<Step>(untrack(() => fromState(initialState)));
	let message = $state('');
	let notDeliveredTo = $state<string[]>([]);
	// Don't send is on its way: the question stays on screen with its buttons held, never 'Sending…'.
	let declining = $state(false);
	const reasonId = $props.id();
	// In Activity the agent that wrote the draft is a link to its chat, at the bar's start.
	const openChat = getContext<((threadId: string) => void) | undefined>(ASKER_CHAT);
	const asker = getContext<{ name: string; id: string } | undefined>(ASKER_WHO);
	const canSend = $derived(connected && !blocker && !readOnly);
	// From the click on, the text on screen is what is being sent: no edit may follow it.
	$effect(() => { ended = step === 'sending' || step === 'sent' || step === 'declined' || step === 'unconfirmed'; });
	onDestroy(() => { ended = false; });

	type Action = 'send' | 'discard';
	function apply(result: DraftActionResult, action: Action) {
		switch (result.state) {
			case 'sent': notDeliveredTo = result.not_delivered_to ?? []; step = 'sent'; break;
			case 'discarded': step = 'declined'; break;
			case 'sending': case 'unconfirmed': step = 'unconfirmed'; break;
			case 'failed': step = 'failed'; break;
			case 'changed': case 'newer':
				message = result.message; step = 'draft';
				// Either refusal re-reads the view: a newer email, or attachments an agent's save changed, show before the next click.
				onReread();
				break;
			case 'signin': case 'missing':
				step = 'draft'; onRefused(result.state); break;
			case 'unknown': return settleFromServer(action);
		}
	}
	/**
	 * The server didn't give an answer (an error status, or the connection dropped): only the draft's stored state says
	 * what happened. After Send now, open means no send holds it, so it didn't leave; sending means it may have.
	 * After Don't send, an open draft goes back to the draft, never to a send failure whose Retry would send it.
	 */
	async function settleFromServer(action: Action) {
		const state = await readState().catch(() => null);
		if (action === 'discard' && (state === null || state === 'open')) {
			step = 'draft';
			message = state === null ? 'Couldn’t confirm Don’t send went through. Press it again if you still don’t want to send it.' : 'Don’t send didn’t go through. The draft is still open.';
			return;
		}
		step = state === null ? 'unconfirmed' : state === 'open' ? 'failed' : fromState(state);
	}
	async function startSend() {
		if (!canSend || (step !== 'draft' && step !== 'failed')) return;
		step = 'sending'; message = '';
		if (!await flush().catch(() => false)) {
			step = 'draft'; message = 'Couldn’t save your last edit, so nothing was sent. Press Send now again.'; return;
		}
		try { await apply(await sendEmailDraft(artifactId, text(), targetVersion, readAt), 'send'); } catch { await settleFromServer('send'); }
	}
	async function decline() {
		if (step !== 'confirm' || declining || !connected || readOnly) return;
		declining = true; message = '';
		try { await apply(await discardEmailDraft(artifactId), 'discard'); } catch { await settleFromServer('discard'); } finally { declining = false; }
	}
</script>

<div class="email-send-bar" role="group" aria-label="Send this email">
	{#if openChat && asker && (step === 'draft' || step === 'failed')}
		<button type="button" class="asker-chat" aria-label={`Open chat with ${asker.name}`} onclick={() => openChat(asker.id)}>{asker.name}</button>
	{/if}
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
			<Button size="sm" variant="primary" ariaDisabled={!canSend} describedby={canSend ? undefined : reasonId} onclick={startSend}>Send now</Button>
		</span>
	{:else if step === 'confirm'}
		<span class="bar-text" role="status">Don’t send this email?</span>
		<span class="bar-actions"><Button size="sm" variant="ghost" disabled={declining} onclick={() => (step = 'draft')}>Keep draft</Button><Button size="sm" variant="secondary" disabled={declining} onclick={decline}>Don’t send</Button></span>
	{:else if step === 'sending'}
		<span class="bar-text" role="status">Sending…</span>
	{:else if step === 'sent'}
		<span class="bar-text" role="status">Sent{notDeliveredTo.length ? `. ${notDeliveredTo.join(', ')} didn’t get it.` : ''}</span>
	{:else if step === 'failed'}
		<span class="bar-text failed" role="alert">Couldn’t send. The email didn’t leave, and the draft is kept.</span>
		<span class="bar-actions"><Button size="sm" variant="secondary" disabled={!connected || readOnly} onclick={() => (step = 'confirm')}>Don’t send</Button><Button size="sm" variant="primary" ariaDisabled={!canSend} describedby={canSend ? undefined : reasonId} onclick={startSend}>Retry</Button></span>
	{:else if step === 'unconfirmed'}
		<span class="bar-text" role="status">Couldn’t confirm the email left. Check your Sent mail. If it isn’t there, ask the agent for a new draft.</span>
	{:else}
		<span class="bar-text" role="status">Not sent. This draft is marked as not to send.</span>
	{/if}
</div>

<style>
	/* The left inset is the email header's own (EmailNotes), so the bar's text starts where From does. */
	.email-send-bar { flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: var(--space-1) var(--space-2); min-height: var(--space-7); padding: var(--space-1) var(--doc-phone-inset, var(--space-3)) var(--space-1) var(--doc-inset, 24px); border-bottom: 1px solid var(--border-subtle); }
	.bar-text { flex: 1 1 12rem; min-width: 0; font-size: var(--text-footnote); line-height: var(--lh-normal); color: var(--text-secondary); overflow-wrap: anywhere; }
	.asker-chat { flex: 0 1 auto; min-width: 0; margin-right: auto; padding: 0; border: 0; background: none; font: inherit; font-size: var(--text-subhead); color: var(--accent-text); cursor: pointer; text-align: left; overflow-wrap: anywhere; border-radius: var(--radius-xs); }
	.asker-chat:hover { text-decoration: underline; }
	.asker-chat:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* On a phone the name sits at the text edge, left of From:. */
	:global(.activity.phone) .asker-chat { margin-left: calc(var(--space-6) - var(--doc-inset, var(--space-6))); }
	.bar-text.failed { color: var(--red-text); }
	.bar-actions { flex: 0 0 auto; display: inline-flex; gap: var(--space-2); }
	.email-send-bar :global(.btn) { flex: 0 0 auto; }
	@media (pointer: coarse) { .email-send-bar { min-height: var(--touch-target); } }
</style>
