<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import { toasts } from '$lib/stores/toast';
	import { toastAnchorSurface } from '$lib/stores/toastAnchor';
	import { answerReview, fetchReviewBar, ReviewRefusal, sendHoldIds, type ReviewBarRead, type ReviewVerdict } from '$lib/api/reviewBar';
	import { reload as reloadAttentionQueue } from '$lib/stores/attentionDock.svelte';
	import { on } from '$lib/ws';

	// PROTOTYPE STATES ONLY. The person's Send and Don't send on an email draft's canvas, with the five-second Undo.
	// Nothing here reaches a server: the real hold is the backend's (claim at tap, send after five seconds, Undo
	// releases the claim), and this bar only draws what the person would see at each step.
	const HOLD_MS = 5000;
	type Step = 'draft' | 'confirm' | 'sending' | 'sent' | 'failed' | 'declined';
	// `readOnly`: the tab can't edit, so neither action can work. `connected`: the document's live connection, the same
	// state that gates typing. `blocker`: the name of an attachment whose file is gone. `sendEnds` is the prototype's
	// stand-in for the mail account's answer after the hold.
	// `reviewArtifactId`: this draft has a review item in the Needs-you list. Send and Don't send answer only the request its
	// producer marked `purpose: 'send'`: Send answers it as approved and Don’t send as changes requested with a fixed note
	// (the server refuses that verdict without one), by `POST /api/attention/{id}/clear`, outcome `answer`, and the queue is
	// re-read. Any other request on the draft is answered in the canvas's review bar, never here, and so is a send request the bar
// did not know when the person pressed (a read that failed or had not finished): the bar then answers nothing.
	// The email's own result never waits on the answer: the email stays Sent or Not sent, and only the answer can be retried.
	// `ended`: set once the email is sent or marked not to send; the canvas then holds the draft read-only.
	// `answersShown`: the canvas's review bar is showing the request with its answers, so the unanswered sentence may point at it.
	let { readOnly = false, connected = true, blocker = null, sendEnds = 'sent', reviewArtifactId = null, ended = $bindable(false), answersFree = $bindable(false), answersShown = false }: {
		readOnly?: boolean; connected?: boolean; blocker?: string | null; sendEnds?: 'sent' | 'failed'; reviewArtifactId?: string | null; ended?: boolean; answersFree?: boolean; answersShown?: boolean;
	} = $props();
	const NOT_SENT_NOTE = 'This email was marked not to send.';
	// 'pending': the answer is on its way. 'failed': it didn’t arrive and can be tried again, with the same holds. 'ended':
	// the server refused it as `ask_stale` (the request the person answered was replaced or ended); nothing is answered again.
	// 'unanswered': the bar did not know the draft's requests when the person pressed, so it answered nothing; the request is
	// answered in the canvas's review bar, which shows it once the email has ended.
	let review = $state<'idle' | 'pending' | 'failed' | 'ended' | 'unanswered'>('idle');
	// What the bar last read of the draft's requests, or null while it has not completed a read (never read, the read failed,
	// or the first read is still on its way). It only draws state ('a new request waits'); what an answer carries is fixed when
	// the person presses (`target`), never chosen from a later read. Only a read that completed before the press counts.
	let known = $state<ReviewBarRead | null>(null);
	let target: { verdict: ReviewVerdict; holds: string[] | null; seen: string[] } | null = null;
	let seen = $state<string[]>([]);
	// The 'unanswered' sentence holds only while a send request may still be open: once a completed read shows none (the person
	// answered it in the review bar, or it ended), the bar reads plain Sent / Not sent.
	const sendStillOpen = $derived(known === null || (known.requests ? known.requests.some((request) => request.purpose === 'send') : known.ask_raised));
	async function readRequests(): Promise<void> {
		if (!reviewArtifactId) return;
		try { known = await fetchReviewBar(reviewArtifactId); } catch { /* the bar keeps what it last knew; not knowing shows at the press */ }
	}
	onMount(() => {
		void readRequests();
		let timer: ReturnType<typeof setTimeout> | undefined;
		const refresh = () => { clearTimeout(timer); timer = setTimeout(() => void readRequests(), 150); };
		const offs = [on('artifacts_changed', refresh), on('plans_changed', refresh)];
		return () => { clearTimeout(timer); offs.forEach((off) => off?.()); };
	});
	// The person's choice, at the press: the send-request holds the bar knows then are the ones this answer and every retry carry.
	// `holds: null` is not knowing (no read had completed), which is not knowing there is no send request (an empty list).
	function choose(verdict: ReviewVerdict) {
		if (!reviewArtifactId) return;
		const pressed = known ? $state.snapshot(known) : null;
		target = { verdict, holds: pressed ? sendHoldIds(pressed) : null, seen: pressed?.requests?.map((request) => request.hold_id) ?? [] };
	}
	async function settleReview() {
		const id = reviewArtifactId;
		if (!id || !target) return;
		const { verdict, holds, seen: openAtPress } = target;
		seen = openAtPress;
		if (!holds) { review = 'unanswered'; return; } // not known at the press: answer nothing, and say so
		if (!holds.length) { review = 'idle'; return; } // known: no send request was open when the person pressed
		review = 'pending';
		try {
			await answerReview(id, verdict, verdict === 'changes_requested' ? NOT_SENT_NOTE : '', holds);
			review = 'idle';
			reloadAttentionQueue();
		} catch (error) {
			if (!(error instanceof ReviewRefusal && error.code === 'ask_stale')) { review = 'failed'; return; }
			await readRequests(); // to show what waits now, never to choose what to answer
			review = 'ended';
		}
	}
	// A request that was not open when the person pressed is open now: it waits in the canvas's review bar with its own answers.
	const waiting = $derived(known?.requests?.some((request) => !seen.includes(request.hold_id)) ?? false);
	let step = $state<Step>('draft');
	let timer: ReturnType<typeof setTimeout> | null = null;
	const reasonId = $props.id();
	$effect(() => { ended = step === 'sent' || step === 'declined'; });
	// `answersFree`: the email has ended and this bar holds no answer (none on its way, none to retry), so the canvas's review
	// bar may answer a send request. While this bar owns the answer the request has one home.
	$effect(() => { answersFree = (step === 'sent' || step === 'declined') && review !== 'pending' && review !== 'failed'; });
	const canSend = $derived(connected && !blocker);

	function send() {
		if (!canSend) return;
		choose('approved');
		step = 'sending';
		sendEndsAt = Date.now() + HOLD_MS;
		timer = setTimeout(() => { timer = null; step = sendEnds === 'failed' ? 'failed' : 'sent'; if (step === 'sent') void settleReview(); }, HOLD_MS);
	}
	function undo() {
		if (timer) clearTimeout(timer);
		timer = null;
		target = null;
		step = 'draft';
	}
	function decline() {
		choose('changes_requested');
		step = 'declined';
		void settleReview();
	}
	// Undo lives in this bar's own Sending row, so the toolbar, From, To, Cc and Subject stay readable for all five seconds. The
	// ordinary toast offers it only while the bar is not on screen (the canvas is hidden behind another view); it is the same Undo.
	let barEl = $state<HTMLElement | null>(null);
	let barOnScreen = $state(true);
	let sendEndsAt = 0;
	$effect(() => {
		if (!barEl || typeof IntersectionObserver === 'undefined') return;
		const watcher = new IntersectionObserver((entries) => { barOnScreen = entries[entries.length - 1]?.isIntersecting ?? true; });
		watcher.observe(barEl);
		return () => watcher.disconnect();
	});
	$effect(() => {
		if (step !== 'sending' || barOnScreen) return;
		toasts.pin('email-send', {
			message: 'Sending…', type: 'info', duration: Math.max(1, sendEndsAt - Date.now()), closeLabel: 'Dismiss',
			action: { label: 'Undo', run: undo },
		});
		return () => toasts.unpin('email-send');
	});
	onDestroy(() => { if (timer) clearTimeout(timer); toasts.unpin('email-send'); ended = false; answersFree = false; });
</script>

{#snippet outcome(plain: string, lead: string = plain)}
	{#if review === 'pending'}
		<span class="bar-text" role="status">{lead}. Updating the review…</span>
	{:else if review === 'failed'}
		<span class="bar-text failed" role="alert">{lead}. Couldn’t mark the review as answered.</span>
		<span class="bar-actions"><Button size="sm" variant="secondary" onclick={() => void settleReview()}>Mark as answered</Button></span>
	{:else if review === 'unanswered' && sendStillOpen}
		<span class="bar-text" role="status">{lead}. The review request wasn’t answered.{answersShown ? ' Answer it in the review bar.' : ''}</span>
	{:else if review === 'ended'}
		<span class="bar-text" role="status">{waiting ? `${lead}. The earlier review request ended. A new request is waiting in the review bar.` : `${lead}. The review request has ended.`}</span>
	{:else}
		<span class="bar-text" role="status">{plain}</span>
	{/if}
{/snippet}

<div class="email-send-bar" role="group" aria-label="Send this email" use:toastAnchorSurface bind:this={barEl}>
	{#if readOnly && step === 'draft'}
		<span class="bar-text" role="status">Read-only. This draft can’t be sent from here.</span>
	{:else if step === 'draft'}
		{#if !connected}
			<span class="bar-text" id={reasonId} role="status">Offline. Send is available when you reconnect.</span>
		{:else if blocker}
			<span class="bar-text" id={reasonId} role="status">{blocker} is unavailable. Remove it to send.</span>
		{/if}
		<span class="bar-actions">
			<Button size="sm" variant="secondary" disabled={!connected} onclick={() => (step = 'confirm')}>Don’t send</Button>
			<Button size="sm" variant="primary" ariaDisabled={!canSend} describedby={canSend ? undefined : reasonId} onclick={send}>Send</Button>
		</span>
	{:else if step === 'confirm'}
		<span class="bar-text" role="status">Don’t send this email?</span>
		<span class="bar-actions">
			<Button size="sm" variant="ghost" onclick={() => (step = 'draft')}>Keep draft</Button>
			<Button size="sm" variant="secondary" onclick={decline}>Don’t send</Button>
		</span>
	{:else if step === 'sending'}
		<span class="bar-text" role="status">Sending…</span>
		<span class="bar-actions"><Button size="sm" variant="secondary" onclick={undo}>Undo</Button></span>
	{:else if step === 'sent'}
		{@render outcome('Sent')}
	{:else if step === 'failed'}
		<span class="bar-text failed" role="alert">Couldn’t send. The email didn’t leave, and the draft is kept.</span>
		<span class="bar-actions">
			<Button size="sm" variant="secondary" onclick={() => (step = 'confirm')}>Don’t send</Button>
			<Button size="sm" variant="primary" onclick={send}>Retry</Button>
		</span>
	{:else}
		{@render outcome('Not sent. This draft is marked as not to send.', 'Not sent')}
	{/if}
</div>

<style>
	.email-send-bar {
		flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: var(--space-1) var(--space-2);
		min-height: var(--space-7); padding: var(--space-1) var(--doc-phone-inset, var(--space-3)) var(--space-1) var(--doc-inset, 24px);
		border-bottom: 1px solid var(--border-subtle);
	}
	.bar-text { flex: 1 1 12rem; min-width: 0; font-size: var(--text-footnote); line-height: var(--lh-normal); color: var(--text-secondary); overflow-wrap: anywhere; }
	.bar-text.failed { color: var(--red-text); }
	.bar-actions { flex: 0 0 auto; display: inline-flex; gap: var(--space-2); }
	.email-send-bar :global(.btn) { flex: 0 0 auto; }
	@media (pointer: coarse) { .email-send-bar { min-height: var(--touch-target); } }
</style>
