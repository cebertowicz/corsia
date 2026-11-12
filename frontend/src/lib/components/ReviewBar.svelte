<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Button } from '$lib/components/ui';
	import { on } from '$lib/ws';
	import PersonStepReviewBar from './PersonStepReviewBar.svelte';
	import ReviewBarShell from './ReviewBarShell.svelte';
	import { createReviewBarState, DRAFT_OPEN, type ReviewBarState } from '$lib/reviewBarState.svelte';
	import { answerSubmissionFailed, answerSubmitted } from '$lib/stores/cardAnswers';

	/**
	 * The in-view review bar (DCOL-FR12.4): when an agent has asked for a review of this artifact, the
	 * answer is one click from the reading. `draftOpen` is the comment composer's unsaved text.
	 */
	let { artifactId, bar = createReviewBarState(), draftOpen = false, suppressed = false, inSheet = false, capsuleClearance = $bindable(0), onAnswered, pendingSuggestions = 0, onAcceptAll }: {
		artifactId: string | null; bar?: ReviewBarState; draftOpen?: boolean; suppressed?: boolean; inSheet?: boolean;
		/** Open suggestions the bar's own accept applies before it approves; with `onAcceptAll`, the approve button says so. */
		pendingSuggestions?: number;
		/** Accepts them all and puts the accepted text on screen; throws when it could not, so no verdict is sent. */
		onAcceptAll?: () => Promise<void>;
		/** Out: px the bar's container leaves at its bottom so the floating capsule never covers it; 0 while it does not float. */
		capsuleClearance?: number;
		onAnswered?: () => void;
	} = $props();

	const ACCEPT_FAILED = "Couldn't accept the suggestions. Try again.";
	let noteInput = $state<HTMLTextAreaElement>();
	let requestButton = $state<HTMLButtonElement>();
	let changesButton = $state<HTMLButtonElement>();
	/** Reject asks for another review; approval with changes does not. */
	const LABELS = { changes_requested: 'Reject', approved_with_changes: 'Approve with changes' } as const;
	let approveButton = $state<HTMLButtonElement>();
	let refreshTimer: ReturnType<typeof setTimeout> | undefined;

	const read = $derived(bar.read);
	const asker = $derived(read?.asker?.name ?? 'An agent');
	const ask = $derived(read?.ask_reason ?? '');
	const holdIds = $derived(read?.hold_ids ?? []);
	const others = $derived(read?.other_askers ?? []);
	const round = $derived(read?.round_feedback_count ?? 0);
	const pending = $derived(`${round} comment${round === 1 ? '' : 's'}`);
	const busy = $derived(bar.phase === 'sending' || bar.accepting);
	const personStep = $derived(bar.read?.person_step ?? null);
	let personReceipt = $state<'done' | 'wont_do' | null>(null);
	/** A send keeps the layout it came from: the note stays mounted while its own send is in flight. */
	const noting = $derived(bar.phase === 'noting' || (busy && bar.sendingFrom === 'noting'));
	const placeholder = $derived(bar.noteRequired ? `What should ${asker} change?` : `Add a note for ${asker} (optional)`);
	const label = $derived(
		`${asker} asked for your review: ${ask}${others.length ? `. ${others.join(' and ')} also asked.` : ''}`,
	);
	const alsoSends = $derived(round > 0 ? `Also sends your ${pending} to ${asker}` : undefined);
	const approveSends = $derived(round > 0 ? `Your ${pending} ${round === 1 ? 'goes' : 'go'} to ${asker}` : undefined);
	const receiptText = $derived.by(() => {
		const receipt = bar.receipt;
		if (!receipt) return [];
		const said = { approved: 'Approved', approved_with_changes: 'Approved with changes', changes_requested: 'Rejected' }[receipt.verdict];
		const time = receipt.at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
		const comments = receipt.sent.comments;
		const delivered = receipt.verdict === 'approved' && comments > 0
			? `${comments} comment${comments === 1 ? '' : 's'} sent` : '';
		return [said, delivered, time].filter(Boolean);
	});
	const view = $derived(bar.phase === 'answered' ? 'receipt' : bar.phase === 'stale' ? 'stale' : 'ask');

	// A new artifact starts a new bar; the read follows the artifact's and the plans' changes.
	$effect(() => {
		if (bar.setTarget(artifactId)) void bar.load();
	});
	$effect(() => {
		void artifactId;
		personReceipt = null;
	});
	$effect(() => {
		if (!draftOpen && bar.failure === DRAFT_OPEN) bar.failure = '';
	});
	onMount(() => {
		const refresh = () => {
			clearTimeout(refreshTimer);
			refreshTimer = setTimeout(() => void bar.load(), 150);
		};
		const offs = [on('artifacts_changed', refresh), on('plans_changed', refresh)];
		return () => { clearTimeout(refreshTimer); offs.forEach((off) => off?.()); };
	});
	// The note grows with its text to the CSS cap, then keeps an ending caret visible while it scrolls.
	$effect(() => {
		void bar.note;
		void tick().then(() => {
			if (!noteInput) return;
			noteInput.style.height = 'auto';
			noteInput.style.height = `${noteInput.scrollHeight}px`;
			if (document.activeElement === noteInput && noteInput.selectionEnd === noteInput.value.length) noteInput.scrollTop = noteInput.scrollHeight;
		});
	});

	function blocked(): boolean {
		if (!draftOpen) return false;
		bar.failure = DRAFT_OPEN;
		return true;
	}
	/** The answer closes the user's feedback batch on the server; the viewer's pending-batch state re-reads it. */
	async function settle(verdict: 'approved' | 'approved_with_changes' | 'changes_requested') {
		const targetId = artifactId;
		if (!targetId) return;
		const targetHoldIds = [...holdIds];
		const outcome = await bar.answer(
			verdict,
			() => answerSubmitted(targetHoldIds, { state: verdict, feedback: bar.read?.round_feedback_counts }),
			(sent) => answerSubmitted(targetHoldIds, { state: sent.verdict, feedback: sent.feedback_counts }),
		);
		if (outcome === 'sent') {
			if (bar.artifactId === targetId) onAnswered?.();
			return;
		}
		if (outcome === 'failed') answerSubmissionFailed(targetHoldIds);
		if (outcome === 'ignored') return;
		// A failed send that came back without a receipt leaves focus on the control that sent it.
		await tick();
		if (bar.phase === 'noting') noteInput?.focus();
		else if (bar.phase === 'ask') approveButton?.focus();
	}
	const acceptFirst = $derived(pendingSuggestions > 0 && onAcceptAll !== undefined);
	const approveText = $derived(acceptFirst ? 'Approve all' : 'Approve');
	const approveDescription = $derived(acceptFirst ? `Accepts the ${pendingSuggestions} suggestion${pendingSuggestions === 1 ? '' : 's'}, then approves` : approveSends);
	async function approve() {
		if (round > 0 && blocked()) return;
		if (acceptFirst) {
			bar.accepting = true; bar.failure = '';
			try { await onAcceptAll?.(); } catch { bar.failure = ACCEPT_FAILED; return; } finally { bar.accepting = false; }
		}
		void settle('approved');
	}
	/** Approve with changes sends at once when the round has feedback; otherwise it needs a note, like Reject. */
	async function approveWithChanges() {
		if (blocked()) return;
		if (round > 0) { void settle('approved_with_changes'); return; }
		bar.noteFor = 'approved_with_changes';
		bar.openNote();
		await tick();
		noteInput?.focus();
	}
	async function requestChanges() {
		if (blocked()) return;
		bar.noteFor = 'changes_requested';
		bar.openNote();
		await tick();
		noteInput?.focus();
	}
	async function cancelNote() {
		bar.cancelNote();
		await tick();
		(bar.noteFor === 'approved_with_changes' ? changesButton : requestButton)?.focus();
	}
	function sendNote() {
		if (blocked()) return;
		void settle(bar.noteFor);
	}
	function onNoteKey(event: KeyboardEvent) {
		if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendNote(); }
		else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); void cancelNote(); }
	}
</script>

{#if personStep || personReceipt}
	<PersonStepReviewBar {personStep} bind:receipt={personReceipt} {suppressed} bind:capsuleClearance {onAnswered} onConflict={() => void bar.load()} />
{:else if bar.visible}
	<ReviewBarShell
		{view} who={{ name: asker, id: read?.asker?.id ?? asker }} plus={others.length} {ask} full={ask} lineLabel={label}
		others={read?.other_asks ?? []} askLabel="Review request" {noting} {busy} status={busy ? 'Sending your review…' : bar.failure}
		{suppressed} {inSheet} bind:capsuleClearance
		receipt={{ icon: bar.receipt?.verdict === 'changes_requested' ? 'changes' : 'check', label: 'Review answered', segments: receiptText }}
		stale={{ label: 'Review request ended', text: 'This review request has ended', sub: 'Refreshing' }}
	>
		{#snippet actions()}
			{#if noting}
				<textarea class="rb-note" rows="1" aria-label={`Note for ${asker}`} {placeholder} maxlength="2000" readonly={busy}
					bind:this={noteInput} bind:value={bar.note} onkeydown={onNoteKey}></textarea>
				<div class="rb-actions">
					<Button size="sm" variant="ghost" disabled={busy} onclick={cancelNote}>Cancel</Button>
					<Button size="sm" variant="primary" loading={busy} disabled={bar.noteRequired && !bar.note.trim()} onclick={sendNote}>{LABELS[bar.noteFor]}</Button>
				</div>
			{:else}
				<div class="rb-actions">
					<Button size="sm" variant="ghost" disabled={busy} bind:el={requestButton} onclick={requestChanges}>{LABELS.changes_requested}</Button>
					<Button size="sm" variant="secondary" disabled={busy} bind:el={changesButton} description={alsoSends} title={alsoSends} onclick={approveWithChanges}>{LABELS.approved_with_changes}</Button>
					<Button size="sm" variant="primary" loading={busy} bind:el={approveButton} description={approveDescription} title={approveDescription} onclick={approve}>{approveText}</Button>
				</div>
			{/if}
		{/snippet}
	</ReviewBarShell>
{/if}
