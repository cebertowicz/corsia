<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Avatar, Button } from '$lib/components/ui';
	import { agentColor } from '$lib/docPresence';
	import { on } from '$lib/ws';
	import { createReviewBarState, DRAFT_OPEN } from '$lib/reviewBarState.svelte';

	/**
	 * The in-view review bar (DCOL-FR12.4): when an agent has asked for a review of this artifact, the
	 * answer is one click from the reading. `draftOpen` is the comment composer's unsaved text.
	 */
	let { artifactId, draftOpen = false, onAnswered }: { artifactId: string | null; draftOpen?: boolean; onAnswered?: () => void } = $props();

	const bar = createReviewBarState();
	let expanded = $state(false);
	let noteInput = $state<HTMLInputElement>();
	let requestButton = $state<HTMLButtonElement>();
	let receiptEl = $state<HTMLElement>();
	let refreshTimer: ReturnType<typeof setTimeout> | undefined;

	const read = $derived(bar.read);
	const asker = $derived(read?.asker?.name ?? 'An agent');
	const ask = $derived(read?.ask_reason ?? '');
	const others = $derived(read?.other_askers ?? []);
	const round = $derived(read?.round_feedback_count ?? 0);
	const pending = $derived(`${round} comment${round === 1 ? '' : 's'}`);
	const busy = $derived(bar.phase === 'sending');
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
		const sent = receipt.sent ? `sent ${receipt.sent} comment${receipt.sent === 1 ? '' : 's'}` : '';
		return [said, sent, time].filter(Boolean);
	});

	// A new artifact starts a new bar; the read follows the artifact's and the plans' changes.
	$effect(() => {
		if (bar.setTarget(artifactId)) void bar.load();
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
	async function settle(sent: Promise<boolean>) {
		if (await sent) onAnswered?.();
	}
	function approve() {
		if (round > 0 && blocked()) return;
		void settle(bar.answer(round > 0 ? 'approved_with_changes' : 'approved'));
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
		void settle(bar.answer('changes_requested'));
	}
	function onNoteKey(event: KeyboardEvent) {
		if (event.key === 'Enter') { event.preventDefault(); sendNote(); }
		else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); void cancelNote(); }
	}
</script>

{#if bar.visible}
	{#if bar.phase === 'answered'}
		<section class="review-bar receipt" class:changes={bar.receipt?.verdict === 'changes_requested'} aria-label="Review answered">
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
		<section class="review-bar stale" role="status" aria-label="Review request replaced">
			<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M13 8a5 5 0 11-1.5-3.6M13 2.5v3h-3" /></svg>
			<span class="rb-text">This review request was replaced</span><span class="rb-sub">Refreshing</span>
		</section>
	{:else}
		{#snippet avatar()}<Avatar name={asker} color={agentColor(read?.asker?.id ?? asker)} hidden />{/snippet}
		<section class="review-bar" class:noting={bar.phase === 'noting'} class:expanded aria-label="Review request" aria-busy={busy}>
			<span class="rb-head">
				<span class="rb-wide rb-head">
					{@render avatar()}
					<!-- svelte-ignore a11y_no_noninteractive_tabindex: the truncated ask is focusable so keyboard users can read it in full via its title. -->
					<span class="rb-line" tabindex="0" title={ask}><strong>{asker}</strong> asked for your review<span class="rb-ask">{ask}</span></span>
					{#if others.length === 1}
						<span class="rb-more">{others[0]} also asked</span>
					{:else if others.length > 1}
						<span class="rb-more" title={others.join(', ')}>+{others.length} more</span>
					{/if}
				</span>
				<button type="button" class="rb-narrow" aria-expanded={expanded} aria-label={label} onclick={() => (expanded = !expanded)}>
					{@render avatar()}
					<span class="rb-narrow-text"><strong>{asker}</strong>{#if others.length}<span class="rb-plus" aria-hidden="true">+{others.length}</span>{/if}: <span class="rb-ask">{ask}</span></span>
					<svg class="rb-icon rb-chev" aria-hidden="true" viewBox="0 0 16 16"><path d="M4 6.5l4 4 4-4" /></svg>
				</button>
				{#if expanded && read?.other_asks.length}
					<ul class="rb-others">
						{#each read.other_asks as other (other.name)}<li><strong>{other.name}</strong>: {other.reason}</li>{/each}
					</ul>
				{/if}
			</span>
			<span class="rb-failure" role="status">{bar.failure}</span>
			{#if bar.phase === 'noting'}
				<input class="rb-note" type="text" aria-label={`Note for ${asker}`} {placeholder} maxlength="2000"
					bind:this={noteInput} bind:value={bar.note} onkeydown={onNoteKey} />
				<div class="rb-actions">
					<Button size="sm" variant="primary" disabled={bar.noteRequired && !bar.note.trim()} onclick={sendNote}>Request changes</Button>
					<Button size="sm" variant="ghost" onclick={cancelNote}>Cancel</Button>
				</div>
			{:else}
				<div class="rb-actions">
					<Button size="sm" variant="primary" loading={busy} description={alsoSends} title={alsoSends} onclick={approve}>{bar.approveLabel}</Button>
					<Button size="sm" variant="secondary" disabled={busy} bind:el={requestButton} onclick={requestChanges}>Request changes</Button>
				</div>
			{/if}
			{#if busy}<span class="sr-only" role="status">Sending your review</span>{/if}
		</section>
	{/if}
{/if}

<style>
	.review-bar { flex: none; display: flex; align-items: center; gap: 6px 12px; min-height: 40px; padding: 6px 8px 6px 12px; border: 1px solid var(--violet-line); border-radius: var(--radius-sm); background: var(--violet-bg); font-size: var(--text-footnote); color: var(--text-secondary); min-width: 0; }
	.rb-head { display: contents; }
	.rb-narrow, .rb-others { display: none; }
	.rb-line { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
	.rb-line strong { font-weight: var(--fw-semibold); }
	.rb-ask { margin-left: 8px; color: var(--text-secondary); }
	.review-bar.noting .rb-line { flex: 0 1 auto; max-width: 46%; }
	.rb-more { flex: none; white-space: nowrap; color: var(--text-secondary); }
	.rb-actions { display: inline-flex; align-items: center; gap: 6px; flex: none; margin-left: auto; }
	.rb-note { flex: 1 1 14rem; min-width: 0; height: 28px; padding: 0 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-primary); color: var(--text-primary); font-family: var(--font-sans); font-size: var(--text-footnote); }
	.rb-note::placeholder { color: var(--text-tertiary); }
	.rb-note:focus { outline: 2px solid var(--accent); outline-offset: -2px; border-color: transparent; }
	.rb-icon { width: 14px; height: 14px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; stroke-linecap: round; }
	.rb-failure { flex: none; color: var(--amber-text); }
	.rb-failure:empty { display: none; }
	.review-bar.receipt { background: var(--bg-secondary); border-color: var(--border-subtle); color: var(--text-secondary); }
	.review-bar.receipt .rb-icon { color: var(--green-text); }
	.review-bar.receipt.changes .rb-icon { color: var(--amber-text); }
	.review-bar.receipt .rb-text { color: var(--text-primary); font-weight: var(--fw-medium); }
	.rb-receipt { flex: 1 1 0; min-width: 0; outline: none; }
	.rb-seg { white-space: nowrap; }
	.review-bar.stale { background: var(--amber-bg); border-color: var(--amber-line); color: var(--amber-text); }
	.review-bar.stale .rb-text { font-weight: var(--fw-medium); }
	.review-bar.stale .rb-sub { margin-left: auto; opacity: 0.85; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }

	/* The bar sits in the document header, whose left padding is the title's inset, so it aligns with the
	   title at width. Narrow canvas (the same 600px threshold as the status row): the bar spans the canvas
	   with the page gutter instead, by cancelling that inset; row 1 is one button that expands the ask in
	   place, row 2 holds the actions. */
	@container doc-canvas (max-width: 600px) {
		.review-bar { flex-wrap: wrap; padding: 6px 8px; margin-left: calc(var(--doc-rail, 44px) * -1); }
		.review-bar.receipt, .review-bar.stale { flex-wrap: nowrap; align-items: center; min-height: 44px; }
		.rb-wide { display: none; }
		.rb-narrow { position: relative; display: flex; align-items: flex-start; gap: 8px; flex: 1 1 100%; min-width: 0; width: 100%; padding: 0; border: 0; background: none; color: var(--text-primary); font: inherit; text-align: left; cursor: pointer; border-radius: var(--radius-sm); }
		/* The hit area reaches through the bar's padding and the gap above row 2 without changing its height. */
		.rb-narrow::after { content: ''; position: absolute; inset: -6px 0; }
		.rb-narrow:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
		.rb-narrow-text { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 19px; }
		.rb-narrow-text .rb-ask { margin-left: 0; }
		.rb-plus { margin-left: 4px; color: var(--text-secondary); font-weight: var(--fw-semibold); }
		.rb-chev { margin-top: 2px; color: var(--text-tertiary); transition: transform 0.15s ease; }
		.review-bar.expanded .rb-chev { transform: rotate(180deg); }
		.review-bar.expanded .rb-narrow-text { display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; white-space: normal; }
		.review-bar.expanded .rb-others { display: block; flex: 1 1 100%; margin: 0; padding: 0 0 0 27px; list-style: none; color: var(--text-secondary); line-height: 19px; }
		.rb-others li + li { margin-top: 4px; }
		.rb-others strong { color: var(--text-primary); font-weight: var(--fw-semibold); }
		.rb-actions { flex: 1 1 100%; margin-left: 0; }
		.rb-actions :global(.btn) { flex: 1 1 0; min-width: 0; min-height: 44px; }
		/* Note open: the input takes row 1 at full width; the buttons take row 2, so the bar keeps its height. */
		.review-bar.noting .rb-head { display: none; }
		.review-bar.noting .rb-note { flex: 1 1 100%; width: 100%; min-height: 28px; }
		.rb-failure { flex: 1 1 100%; }
		.rb-sub { display: none; }
	}
</style>
