<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { get } from 'svelte/store';
	import { cubicIn } from 'svelte/easing';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { Avatar, Button } from '$lib/components/ui';
	import MiddleName from './MiddleName.svelte';
	import { agentColor } from '$lib/docPresence';
	import { on } from '$lib/ws';
	import PersonStepReviewBar from './PersonStepReviewBar.svelte';
	import { createReviewBarState, DRAFT_OPEN } from '$lib/reviewBarState.svelte';
	import { answerSubmissionFailed, answerSubmitted } from '$lib/stores/cardAnswers';

	/**
	 * The in-view review bar (DCOL-FR12.4): when an agent has asked for a review of this artifact, the
	 * answer is one click from the reading. `draftOpen` is the comment composer's unsaved text.
	 */
	let { artifactId, draftOpen = false, suppressed = false, capsuleClearance = $bindable(0), onAnswered }: {
		artifactId: string | null; draftOpen?: boolean; suppressed?: boolean;
		/** Out: px the bar's container leaves at its bottom so the floating capsule never covers it; 0 while it does not float. */
		capsuleClearance?: number;
		onAnswered?: () => void;
	} = $props();

	const bar = createReviewBarState();
	let expanded = $state(false);
	let noteInput = $state<HTMLTextAreaElement>();
	let requestButton = $state<HTMLButtonElement>();
	let approveButton = $state<HTMLButtonElement>();
	let receiptEl = $state<HTMLElement>();
	let slotEl = $state<HTMLElement>();
	let capsuleEl = $state<HTMLElement>();
	let refreshTimer: ReturnType<typeof setTimeout> | undefined;

	const read = $derived(bar.read);
	const asker = $derived(read?.asker?.name ?? 'An agent');
	const ask = $derived(read?.ask_reason ?? '');
	const holdIds = $derived(read?.hold_ids ?? []);
	const others = $derived(read?.other_askers ?? []);
	const round = $derived(read?.round_feedback_count ?? 0);
	const pending = $derived(`${round} comment${round === 1 ? '' : 's'}`);
	const busy = $derived(bar.phase === 'sending');
	const personStep = $derived(bar.read?.person_step ?? null);
	let personReceipt = $state<'done' | 'wont_do' | null>(null);
	/** A send keeps the layout it came from: the note stays mounted while its own send is in flight. */
	const noting = $derived(bar.phase === 'noting' || (busy && bar.sendingFrom === 'noting'));
	const placeholder = $derived(bar.noteRequired ? `What should ${asker} change?` : `Add a note for ${asker} (optional)`);
	const label = $derived(
		`${asker} asked for your review: ${ask}${others.length ? `. ${others.join(' and ')} also asked.` : ''}`,
	);
	const alsoSends = $derived(round > 0 ? `Also sends your ${pending} to ${asker}` : undefined);
	const receiptText = $derived.by(() => {
		const receipt = bar.receipt;
		if (!receipt) return [];
		const said = { approved: 'You approved this', approved_with_changes: 'You approved this with changes', changes_requested: 'You requested changes' }[receipt.verdict];
		const time = receipt.at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
		const delivered = (['comments', 'suggestions', 'edits'] as const)
			.map((kind) => [kind, receipt.sent[kind]] as const)
			.filter(([, count]) => count > 0)
			.map(([kind, count]) => `${count} ${kind === 'edits' ? 'edit' : kind.slice(0, -1)}${count === 1 ? '' : 's'}`)
			.join(', ');
		return [said, delivered ? `sent ${delivered}` : '', time].filter(Boolean);
	});

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
	// The note grows with its text: a line on a wide canvas, up to four in the phone capsule (the CSS caps the height).
	$effect(() => {
		void bar.note;
		void tick().then(() => { if (!noteInput) return; noteInput.style.height = 'auto'; noteInput.style.height = `${noteInput.scrollHeight}px`; });
	});
	// Focus follows the bar's own state changes: the note when it opens, the receipt once confirmed.
	$effect(() => {
		if (bar.phase === 'answered') void tick().then(() => receiptEl?.focus());
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
	function approve() {
		if (round > 0 && blocked()) return;
		void settle(round > 0 ? 'approved_with_changes' : 'approved');
	}
	async function requestChanges() {
		if (blocked()) return;
		bar.openNote();
		await tick();
		noteInput?.focus();
	}
	async function cancelNote() {
		bar.cancelNote();
		await tick();
		requestButton?.focus();
	}
	function sendNote() {
		if (blocked()) return;
		void settle('changes_requested');
	}
	const token = (name: string) => Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
	/** A new state replaces the old in place: the old row fades out as the new fades in, stacked in one grid cell, so the slot keeps its height (motion.md, content swaps in place). `content` fades only the ask's in-flow text, never the floating capsule, which has its own slide. */
	function swap(node: Element, { leaving, content = false }: { leaving: boolean; content?: boolean }) {
		const duration = token('--dur-tight') * (leaving ? 0.7 : 1);
		if (content) return { duration, tick: (t: number) => (node as HTMLElement).style.setProperty('--rb-fade', String(t)) };
		return { duration, css: (t: number) => `opacity: ${t}` };
	}
	/** The capsule leaves by sliding back below the screen edge, opaque all the way; its entrance is the CSS keyframes below. It slides only when an answer ends the ask: a global transition keeps the whole viewer in the DOM until it ends, so a bar torn down with its viewer leaves at once. */
	function capsuleOut(node: Element) {
		const capsule = node as HTMLElement;
		if (get(prefersReducedMotion) || (bar.phase !== 'answered' && bar.phase !== 'stale') || getComputedStyle(capsule).position !== 'fixed') return { duration: 0 };
		const distance = window.innerHeight - capsule.getBoundingClientRect().top;
		return { duration: token('--dur-standard') * 0.7, easing: cubicIn, css: (_t: number, u: number) => `transform: translateY(${u * distance}px)` };
	}
	/**
	 * How far the floating capsule (a touch screen at a narrow canvas) reaches up into the bar's container (its offset
	 * parent, else the window), plus the capsule's side inset as the gap above it. Zero while the capsule sits in the
	 * bar's own row, is tucked away, or is not there.
	 */
	function measureClearance(): number {
		const capsule = capsuleEl;
		if (!capsule || suppressed) return 0;
		const style = getComputedStyle(capsule);
		if (style.position !== 'fixed') return 0;
		// From its layout box, not its rect: the capsule slides in by a transform, which would move the measured top.
		const top = window.innerHeight - (Number.parseFloat(style.bottom) || 0) - capsule.offsetHeight;
		const container = slotEl?.offsetParent;
		const reach = (container ? container.getBoundingClientRect().bottom : window.innerHeight) - top;
		return reach > 0 ? Math.round(reach + (Number.parseFloat(style.left) || 0)) : 0;
	}
	// The capsule's own size (it grows with a status or the note) and the window's decide whether and how far it
	// floats over the container; its tucked-away state is the `suppressed` prop, read by the measure.
	$effect(() => {
		const capsule = capsuleEl;
		const update = () => { capsuleClearance = measureClearance(); };
		update();
		if (!capsule) return;
		const sizes = typeof ResizeObserver === 'function' ? new ResizeObserver(update) : null;
		sizes?.observe(capsule);
		window.addEventListener('resize', update);
		return () => {
			sizes?.disconnect();
			window.removeEventListener('resize', update);
		};
	});
	function onNoteKey(event: KeyboardEvent) {
		if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendNote(); }
		else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); void cancelNote(); }
	}
</script>

{#if personStep || personReceipt || bar.visible}
	<!-- The slot outlives the phase: it grows once when the first ask arrives, and an answer or a replaced notice swaps inside it at the same height. -->
	<div class="rb-slot" bind:this={slotEl}>
	{#if personStep || personReceipt}
		<PersonStepReviewBar {personStep} bind:receipt={personReceipt} {onAnswered} onConflict={() => void bar.load()} />
	{:else if bar.phase === 'answered'}
		<section class="review-bar receipt" class:changes={bar.receipt?.verdict === 'changes_requested'} aria-label="Review answered" in:swap={{ leaving: false }} out:swap={{ leaving: true }}>
			<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16">
				{#if bar.receipt?.verdict === 'changes_requested'}
					<path d="M3 13l.6-2.8 6.9-6.9a1.4 1.4 0 012 2l-6.9 6.9L3 13z" />
				{:else}
					<circle cx="8" cy="8" r="6" /><path d="M5.3 8.2l1.9 1.9 3.5-3.9" />
				{/if}
			</svg>
			<span class="rb-receipt" role="status" tabindex="-1" bind:this={receiptEl}>
				{#each receiptText as segment, index (segment)}
					<span class="rb-seg" class:rb-text={index === 0}>{segment}{index < receiptText.length - 1 ? ' ·' : ''}</span>{' '}
				{/each}
			</span>
		</section>
	{:else if bar.phase === 'stale'}
		<section class="review-bar stale" role="status" aria-label="Review request ended" in:swap={{ leaving: false }} out:swap={{ leaving: true }}>
			<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M13 8a5 5 0 11-1.5-3.6M13 2.5v3h-3" /></svg>
			<span class="rb-text">This review request has ended</span><span class="rb-sub">Refreshing</span>
		</section>
	{:else}
		<section class="review-bar ask" class:noting class:expanded aria-label="Review request" aria-busy={busy} in:swap={{ leaving: false, content: true }} out:swap={{ leaving: true, content: true }}>
			<span class="rb-head">
				<button type="button" class="rb-line" aria-expanded={expanded} aria-label={label} title={ask} onclick={() => (expanded = !expanded)}>
					<span class="rb-lead">
						<Avatar name={asker} color={agentColor(read?.asker?.id ?? asker)} hidden />
						<span class="rb-who"><strong><MiddleName name={asker} /></strong>{#if others.length}<span class="rb-plus" aria-hidden="true">+{others.length}</span>{/if}:</span>
					</span>
					<span class="rb-ask">{ask}</span>
					<svg class="rb-icon rb-chev" aria-hidden="true" viewBox="0 0 16 16"><path d="M4 6.5l4 4 4-4" /></svg>
				</button>
				{#if expanded}<p class="rb-ask-full">{ask}</p>{/if}
				{#if expanded && read?.other_asks.length}
					<ul class="rb-others">
						{#each read.other_asks as other (other.name)}<li><strong>{other.name}</strong>: {other.reason}</li>{/each}
					</ul>
				{/if}
			</span>
			<span class="rb-row2">
				<div class="rb-capsule" class:suppressed bind:this={capsuleEl} out:capsuleOut|global>
					<!-- At a fine pointer the status keeps its height when empty, so the buttons never move when a status appears. On a phone it shows above the buttons inside the floating capsule, which grows upward. -->
					<span class="rb-status" class:busy role="status">{busy ? 'Sending your review…' : bar.failure}</span>
					{#if noting}
						<textarea class="rb-note" rows="1" aria-label={`Note for ${asker}`} {placeholder} maxlength="2000" readonly={busy}
							bind:this={noteInput} bind:value={bar.note} onkeydown={onNoteKey}></textarea>
						<div class="rb-actions">
							<Button size="sm" variant="ghost" disabled={busy} onclick={cancelNote}>Cancel</Button>
							<Button size="sm" variant="primary" loading={busy} disabled={bar.noteRequired && !bar.note.trim()} onclick={sendNote}>Request changes</Button>
						</div>
					{:else}
						<div class="rb-actions">
							<Button size="sm" variant="ghost" disabled={busy} bind:el={requestButton} onclick={requestChanges}>Request changes</Button>
							<Button size="sm" variant="primary" loading={busy} bind:el={approveButton} description={alsoSends} title={alsoSends} onclick={approve}>{bar.approveLabel}</Button>
						</div>
					{/if}
				</div>
			</span>
		</section>
	{/if}
	</div>
{/if}

<style>
	/* One flat row inside the document header's chrome: no card, border or shadow of its own. */
	.review-bar { --rb-hit: calc(var(--space-8) - var(--space-1)); flex: none; display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); padding: 0 var(--space-1) 0 0; font-size: var(--text-footnote); color: var(--text-secondary); min-width: 0; }
	/* The block grows from nothing (height through grid rows, clipped only while it moves) so the text below slides down instead of jumping; its own margin cancels the header's gap until it has height. An answer swaps in at the block's own height: the rows share one grid cell and crossfade (the transitions in the script). No transform on any ancestor of the capsule: it would make the floating capsule position against the row instead of the viewport. */
	.rb-slot { display: grid; grid-template-rows: 1fr; min-width: 0; animation: rb-grow var(--dur-standard) var(--ease-entrance) backwards, rb-clip var(--dur-standard) step-end; }
	/* `--rb-fade` rests at 1; the swap transition's tick overrides it inline on this same element while the ask's text crossfades. */
	.rb-slot > .review-bar { grid-area: 1 / 1; min-height: 0; --rb-fade: 1; }
	.rb-head, .rb-row2 { opacity: var(--rb-fade); }
	@keyframes rb-grow { from { grid-template-rows: 0fr; margin-top: calc(var(--space-1) * -1); opacity: 0; } }
	@keyframes rb-grow-solid { from { grid-template-rows: 0fr; margin-top: calc(var(--space-1) * -1); } }
	@keyframes rb-clip { from, to { overflow: hidden; } }
	@keyframes rb-capsule-in { from { transform: translateY(calc(100% + var(--space-3) + var(--safe-bottom))); } }
	.review-bar.receipt, .review-bar.stale { min-height: calc(var(--space-7) * 2); }
	/* The ask block is two lines at every width. Line 1: the asker, the ask on one line and the expand chevron (D8).
	   Line 2: a status slot on the left and the answer on the right. */
	/* The canvas already owns the document-review surface. Keep this row in its
	   existing header chrome; raised needs-you cards live only on chat objects. */
	.review-bar.ask {
		flex-direction: column; align-items: stretch; gap: 0;
		padding: 0 var(--space-1) 0 0;
		border: 0; border-radius: 0;
		background: transparent; box-shadow: none;
	}
	.rb-head { display: flex; flex-direction: column; min-width: 0; }
	.rb-line { display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); min-width: 0; padding: 0; border: 0; background: none; color: var(--text-primary); font: inherit; text-align: left; cursor: pointer; border-radius: var(--radius-sm); }
	.rb-line:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* The avatar and name; it lays nothing out itself except in the narrow header below. */
	.rb-lead { display: contents; }
	/* The requester takes its natural width up to the header cap, leaving the ask the remaining one-line space. */
	.rb-who { flex: 0 1 auto; display: flex; min-width: 0; max-width: 55%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.rb-who strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: var(--fw-semibold); }
	/* This wrapper constrains the name; MiddleName owns its head/tail truncation within it. */
	.rb-plus { margin-left: var(--space-1); color: var(--text-secondary); font-weight: var(--fw-semibold); }
	.rb-ask { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); }
	.rb-chev { color: var(--text-tertiary); transition: transform var(--dur-tight) var(--ease-snap); }
	.review-bar.expanded .rb-chev { transform: rotate(180deg); }
	/* Expanded, line 1 keeps its one row and the text moves below it, across the whole row (PE round 5 F-C). */
	.review-bar.expanded .rb-ask { display: none; }
	.review-bar.expanded .rb-chev { margin-left: auto; }
	.rb-ask-full { margin: 0; padding: 0 0 var(--space-1); color: var(--text-secondary); line-height: var(--lh-normal); overflow-wrap: anywhere; }
	.rb-others { margin: 0; padding: 0 0 var(--space-1) var(--space-6); list-style: none; color: var(--text-secondary); }
	.rb-others li + li { margin-top: var(--space-1); }
	.rb-others strong { color: var(--text-primary); font-weight: var(--fw-semibold); }
	.rb-row2 { display: flex; align-items: center; gap: var(--space-2); min-width: 0; min-height: var(--space-7); }
	.rb-status { flex: 1 1 0; min-width: 0; color: var(--amber-text); }
	.rb-status.busy { color: var(--text-secondary); }
	.review-bar.noting .rb-row2 { flex-wrap: wrap; }
	.review-bar.noting .rb-status { order: 3; flex-basis: 100%; min-height: 0; }
	.rb-capsule { display: contents; }
	.rb-actions { display: inline-flex; align-items: center; gap: var(--space-1); flex: none; margin-left: auto; }
	/* The note is one line on a wide canvas; the script sizes it to its text and the CSS caps it (four lines in the phone capsule). */
	.rb-note { flex: 1 1 10rem; min-width: 0; box-sizing: border-box; height: var(--space-6); min-height: var(--space-6); max-height: var(--space-6); padding: 0 var(--space-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--text-primary) 5%, transparent); color: var(--text-primary); font-family: var(--font-sans); font-size: var(--text-footnote); line-height: var(--space-6); resize: none; overflow-y: auto; }
	.rb-note::placeholder { color: var(--text-tertiary); }
	.rb-note:focus { outline: 2px solid var(--accent); outline-offset: -2px; border-color: transparent; }
	.rb-icon { width: var(--text-subhead); height: var(--text-subhead); flex: none; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; stroke-linecap: round; }
	.review-bar.receipt .rb-icon { color: var(--green-text); }
	.review-bar.receipt.changes .rb-icon { color: var(--amber-text); }
	.review-bar.receipt .rb-text { color: var(--text-primary); font-weight: var(--fw-medium); }
	.rb-receipt { flex: 1 1 0; min-width: 0; outline: none; }
	.rb-seg { white-space: nowrap; }
	.review-bar.stale .rb-icon { color: var(--amber-text); }
	.review-bar.stale .rb-text { color: var(--text-primary); font-weight: var(--fw-medium); }
	.review-bar.stale .rb-sub { margin-left: auto; opacity: 0.85; }

	/* In the document header the slot shares the header's columns, so the comments pill and Request review sit at the right end of line 1 (PE M1). The ask's line 1 takes the first column, line 2 spans them all so its buttons end at the header's right edge; a receipt or an ended notice takes the first column at the same height and keeps its text on line 1. */
	:global(.document-header) .rb-slot { grid-template-columns: subgrid; }
	:global(.document-header) .rb-slot > .review-bar { grid-area: 1 / 1 / 2 / 2; }
	:global(.document-header) .rb-slot > .review-bar.ask { grid-column: 1 / -1; display: grid; grid-template-columns: subgrid; }
	:global(.document-header) .review-bar.ask .rb-head { display: contents; }
	:global(.document-header) .review-bar.ask .rb-line { grid-column: 1; }
	:global(.document-header) .review-bar.ask .rb-ask-full, :global(.document-header) .review-bar.ask .rb-others, :global(.document-header) .review-bar.ask .rb-row2 { grid-column: 1 / -1; }
	:global(.document-header) .review-bar.receipt, :global(.document-header) .review-bar.stale { align-items: flex-start; }
	:global(.document-header) .review-bar.receipt .rb-icon, :global(.document-header) .review-bar.stale .rb-icon { margin-block: calc((var(--space-7) - var(--text-subhead)) / 2); }
	:global(.document-header) .rb-receipt, :global(.document-header) .review-bar.stale .rb-text, :global(.document-header) .review-bar.stale .rb-sub { line-height: var(--space-7); }
	/* A 16ch ask needs more room than the prior 48px floor. Below this point the tools keep their row, while the review line keeps the avatar, full name and chevron together and gives the ellipsized ask the next full-width row. The name never truncates; it wraps if even its row is short. Line 2 is unchanged.
	   Threshold, measured at a fine pointer with a narrow canvas (the pill shows its count only):
	     avatar 19 + gap 8 + 21-character name 129 + gap 8 + ask minimum 16ch (122) + gap 8 + chevron 13
	     + two header column gaps 8 + tools 94 (comments pill 52 + gap 4 + Request review 38) = 401px of header content; 408px keeps a small tolerance.
	   Measured header content: 195px at 1280 with the pane overlaid, 345px on a 393 phone, 355px at 1440 with the pane docked,
	   635px with it closed. The first three use this fallback. */
	@container doc-header (width < 408px) {
		:global(.document-header) .review-bar.ask { grid-template-rows: var(--space-7) var(--space-7) auto; }
		:global(.document-header) .review-bar.ask .rb-line { display: grid; grid-column: 1 / -1; grid-row: 1 / span 3; grid-template-columns: minmax(0, 1fr) auto; grid-template-rows: subgrid; }
		:global(.document-header) .review-bar.ask .rb-lead { grid-column: 1; grid-row: 2; display: flex; align-items: center; gap: var(--space-2); min-width: 0; min-height: var(--space-7); }
		:global(.document-header) .review-bar.ask .rb-who { max-width: none; }
		:global(.document-header) .review-bar.ask .rb-chev { grid-column: 2; grid-row: 2; align-self: center; margin-left: 0; }
		:global(.document-header) .review-bar.ask .rb-ask { grid-column: 1 / -1; grid-row: 3; min-height: var(--space-7); }
	}

	/* Expanded, the ask text (`.rb-ask-full`) is its own grid item spanning every column under line 1, so it has the header's full width, not what the pill and Request review leave it. */
	/* A narrow column, at any pointer, gives the note its own full-width row (up to four lines) above Cancel and Request changes, as the phone capsule does. */
	@container doc-canvas (max-width: 600px) {
		.review-bar.noting .rb-note { flex: 1 0 100%; order: 1; height: auto; min-height: var(--space-6); max-height: calc(4lh + 2 * var(--space-1)); padding: var(--space-1) var(--space-2); line-height: var(--lh-normal); }
		.review-bar.noting .rb-actions { flex: 1 0 100%; order: 2; justify-content: flex-end; margin-left: 0; }
	}

	/* Solid, not glass: document text reads through any translucent fill, and the header's backdrop root stops the blur from reaching it. A touch screen at a narrow canvas gets the answer in a floating capsule at the canvas's bottom edge, with the status above its buttons (the capsule grows upward); the ask is one in-flow line. */
	@media (any-pointer: coarse) {
		@container doc-canvas (max-width: 600px) {
			.review-bar { min-height: var(--rb-hit); }
			.review-bar.ask { min-height: 0; }
			.rb-line { min-height: var(--rb-hit); }
			/* A closed ask keeps its existing requester/ask markup in one 44px line: the requester has a bounded natural width and the ask end-ellipsizes in the remaining space. */
			:global(.document-header) .review-bar.ask:not(.expanded) { grid-template-rows: var(--rb-hit); }
			:global(.document-header) .review-bar.ask:not(.expanded) .rb-line { display: flex; grid-column: auto; grid-row: auto; }
			:global(.document-header) .review-bar.ask:not(.expanded) .rb-lead { flex: 0 1 55%; max-width: 55%; display: flex; align-items: center; gap: var(--space-2); min-width: 0; min-height: var(--rb-hit); }
			:global(.document-header) .review-bar.ask:not(.expanded) .rb-who { max-width: 100%; }
			/* Centre the ask's text line within the touch row; its former block line box sat above the requester's centred content. */
			:global(.document-header) .review-bar.ask:not(.expanded) .rb-ask { display: flex; align-items: center; align-self: stretch; flex: 1 1 0; min-width: 0; }
			:global(.document-header) .review-bar.ask:not(.expanded) .rb-chev { margin-left: auto; }
			/* The expanded ask and other askers end at the add-person icon's right edge, not the header's: that tool's 44 px target has a 12 px inner margin (`--space-3`), so the text has the same. */
			:global(.document-header) .review-bar.ask .rb-ask-full, :global(.document-header) .review-bar.ask .rb-others { padding-right: var(--space-3); }
			.review-bar.receipt, .review-bar.stale { min-height: var(--rb-hit); }
			:global(.document-header) .review-bar.receipt, :global(.document-header) .review-bar.stale { align-items: center; }
			:global(.document-header) .review-bar.receipt .rb-icon, :global(.document-header) .review-bar.stale .rb-icon { margin-block: 0; }
			:global(.document-header) .rb-receipt, :global(.document-header) .review-bar.stale .rb-text, :global(.document-header) .review-bar.stale .rb-sub { line-height: inherit; }
			.rb-row2 { display: contents; }
			.rb-slot { animation-name: rb-grow-solid, rb-clip; }
			/* The capsule slides from below the safe area and back, fully opaque the whole way (RSR-3): no ancestor fades, and its own motion is transform only. */
			.rb-capsule { position: fixed; flex-wrap: wrap; z-index: 5; left: var(--space-3); right: var(--space-3); bottom: calc(var(--space-3) + var(--safe-bottom)); display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-xl); background: var(--bg-secondary); box-shadow: var(--glass-elevation-1); animation: rb-capsule-in var(--dur-standard) var(--ease-entrance) backwards; transition: transform var(--dur-standard) var(--ease-entrance), visibility 0s; }
			.rb-capsule.suppressed { transform: translateY(calc(100% + var(--space-3) + var(--safe-bottom))); pointer-events: none; visibility: hidden; transition-duration: calc(var(--dur-standard) * 0.7), 0s; transition-delay: 0s, calc(var(--dur-standard) * 0.7); }
			.review-bar.ask .rb-capsule .rb-status { order: -1; flex: 1 0 100%; padding: 0 var(--space-2); }
			.review-bar.ask .rb-capsule .rb-status:empty { display: none; }
			.rb-capsule .rb-actions { flex: 1 1 auto; margin-left: 0; }
			.rb-capsule :global(.btn) { flex: 1 1 0; min-width: 0; min-height: var(--rb-hit); }
			/* Request changes: the capsule grows upward into two rows, the field across the full width (up to four lines), then Cancel and Request changes right-aligned (PE B3). */
			.rb-capsule .rb-note { flex: 1 0 100%; min-height: var(--rb-hit); max-height: calc(4lh + 2 * var(--space-2)); padding: var(--space-2); font-size: var(--text-input); line-height: var(--lh-normal); }
			.review-bar.noting .rb-capsule .rb-actions { flex: 1 0 100%; justify-content: flex-end; }
			.review-bar.noting .rb-capsule .rb-actions :global(.btn) { flex: 0 0 auto; min-width: max-content; }
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.rb-slot { animation: rb-fade var(--dur-tight) var(--ease-snap) backwards; }
		@keyframes rb-fade { from { opacity: 0; } }
	}
</style>
