<script lang="ts">
	import { getContext, onDestroy, untrack } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import { discardEmailDraft, sendEmailDraft, type DraftActionResult } from '$lib/api/emailDrafts';
	import { apiFetch } from '$lib/api/request';
	import { ACTIVITY_EMAIL_REVIEW, type ActivityEmailReview } from '$lib/activityEmailReview';
	import { EMAIL_DRAFT_SETTLED, type ConversationFailure, type EmailDraftState } from '$lib/emailDraft';
	import { emailDraftHeaders } from '$lib/stores/emailDraftHeaders';

	let { artifactId, draftId = null, text, targetVersion = null, readAt = null, initialState = 'open', readOnly = false, connected = true, blocker = null, ended = $bindable(false), flush = async () => true, readState = async () => null, onReread = () => {}, onRefused = () => {} }: {
		artifactId: string; draftId?: string | null; text: () => string; targetVersion?: string | null; readAt?: string | null; initialState?: EmailDraftState;
		readOnly?: boolean; connected?: boolean; blocker?: string | null; ended?: boolean;
		/** Saves every pending edit; false when the save failed. Send waits on it, so the text it carries is the saved text. */
		flush?: () => Promise<boolean>;
		/** The draft's state as the server holds it, or null when it can't be read. */
		readState?: () => Promise<EmailDraftState | null>;
		onReread?: () => void; onRefused?: (reason: ConversationFailure) => void;
	} = $props();
	const onSettled = getContext<(() => void) | undefined>(EMAIL_DRAFT_SETTLED);
	const review = getContext<ActivityEmailReview | undefined>(ACTIVITY_EMAIL_REVIEW);
	let requestButton = $state<HTMLButtonElement>();
	$effect(() => review?.registerRequest(() => requestButton?.focus()));
	type Step = 'draft' | 'confirm' | 'sending' | 'sent' | 'failed' | 'declined' | 'unconfirmed';
	const fromState = (state: EmailDraftState): Step => state === 'sent' ? 'sent' : state === 'discarded' ? 'declined' : state === 'sending' ? 'unconfirmed' : 'draft';
	let step = $state<Step>(untrack(() => fromState(initialState)));
	let message = $state('');
	let notDeliveredTo = $state<string[]>([]);
	// Don't send is on its way: the question stays on screen with its buttons held, never 'Sending…'.
	let declining = $state(false);
	let acknowledging = $state(false);
	let acknowledgeError = $state('');
	let acknowledged = $state(false);
	async function acknowledge() {
		if (!draftId || acknowledging || acknowledged) return;
		acknowledging = true; acknowledgeError = '';
		try {
			const response = await apiFetch(`/api/attention/drafts/${encodeURIComponent(draftId)}/acknowledge`, { method: 'POST' });
			if (response.status !== 204) throw new Error('Acknowledgement failed');
			acknowledged = true; onSettled?.();
		} catch {
			acknowledgeError = 'Couldn’t mark this done. Try again.';
		} finally { acknowledging = false; }
	}
	const reasonId = $props.id();
	const canSend = $derived(connected && !blocker && !readOnly);
	// From the click on, the text on screen is what is being sent: no edit may follow it.
	$effect(() => { ended = step === 'sending' || step === 'sent' || step === 'declined' || step === 'unconfirmed'; });
	onDestroy(() => { ended = false; });

	// A reconstructed bar must receive the state the server just confirmed.
	function rememberState(state: EmailDraftState) {
		emailDraftHeaders.update((all) => {
			const draft = all[artifactId];
			return draft && (!draftId || draft.id === draftId) ? { ...all, [artifactId]: { ...draft, state } } : all;
		});
	}
	type Action = 'send' | 'discard';
	function apply(result: DraftActionResult, action: Action) {
		switch (result.state) {
			case 'sent':
				rememberState('sent');
				notDeliveredTo = result.not_delivered_to ?? []; step = 'sent';
				if (!notDeliveredTo.length) onSettled?.();
				break;
			case 'discarded': rememberState('discarded'); step = 'declined'; onSettled?.(); break;
			case 'sending': rememberState('sending'); step = 'unconfirmed'; break;
			case 'unconfirmed': step = 'unconfirmed'; break;
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
		if (state) rememberState(state);
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

<div class="email-send-bar" class:activity-email={!!onSettled} class:unconfirmed={step === 'unconfirmed'} class:sent={step === 'sent'} class:review-noting={review?.noting} role="group" aria-label="Send this email">
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
			{#if review?.enabled}<Button size="sm" variant="secondary" bind:el={requestButton} onclick={review.requestChanges}>Send back</Button>{/if}
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
		{#if onSettled && draftId}<span class="bar-actions"><Button size="sm" variant="primary" loading={acknowledging} disabled={acknowledged} onclick={acknowledge}>Done</Button></span>{/if}
		{#if acknowledgeError}<span class="bar-text failed" role="alert">{acknowledgeError}</span>{/if}
	{:else}
		<span class="bar-text" role="status">Not sent. This draft is marked as not to send.</span>
	{/if}
</div>

<style>
	/* The left inset is the email header's own (EmailNotes), so the bar's text starts where From does. */
	.email-send-bar { flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: var(--space-1) var(--space-2); min-height: var(--space-7); padding: var(--space-1) var(--doc-phone-inset, var(--space-3)) var(--space-1) var(--doc-inset, 24px); border-bottom: 1px solid var(--border-subtle); }
	.email-send-bar.activity-email { padding: 0 var(--space-3) var(--space-1) var(--doc-inset, 24px); }
	.email-send-bar.activity-email.unconfirmed, .email-send-bar.activity-email.sent { padding-top: var(--space-1); }
	.email-send-bar.review-noting { display: none; }
	.bar-text { flex: 1 1 12rem; min-width: 0; font-size: var(--text-footnote); line-height: var(--lh-normal); color: var(--text-secondary); overflow-wrap: anywhere; }
	.bar-text.failed { color: var(--red-text); }
	.bar-actions { flex: 0 0 auto; display: inline-flex; gap: var(--space-2); }
	.email-send-bar :global(.btn) { flex: 0 0 auto; }
	@media (pointer: coarse) { .email-send-bar { min-height: var(--touch-target); } }
	@media (max-width: 48rem) {
		/* Expand vertically without overlapping the adjacent actions. */
		.email-send-bar.activity-email :global(.btn::after) { inset: -9px 0; }
	}
</style>
