<script lang="ts">
	import { tick, type Snippet } from 'svelte';
	import Expand from './ui/Expand.svelte';
	import { press } from './ui/press';
	import VisitDivider from './VisitDivider.svelte';
	import BlockLine from './BlockLine.svelte';
	import { createPhraseSwap } from './plans/phraseSwap.svelte';
	import { FADE_MS, UNKNOWN_STEP } from './plans/planLiveStep';
	import { blockEntries } from './transcriptBlockEntries';
	import { liveLabel, NEW_SUFFIX, runToggleName } from './transcriptRunLabel';
	import type { TranscriptMessage } from './messageListModel';
	import type { PauseAnnotation, TranscriptFoldEntry, TranscriptMessageEntry } from './transcriptFocusModel';
	import { runHasMore, type RunLoad, type RunStub } from '$lib/focusRuns';

	let {
		entry,
		label,
		expanded,
		bodyId,
		liveTime = null,
		livePhrase = UNKNOWN_STEP,
		liveDimmed = false,
		load,
		onToggle,
		onLoadRun,
		row,
		pause,
		warning,
		liveActivity,
		notes,
	}: {
		entry: TranscriptFoldEntry;
		label: string;
		expanded: boolean;
		bodyId: string;
		/** A live block's active time so far (`3 min`), null while its start is not proven (A9-A12). */
		liveTime?: string | null;
		/** What the running turn is doing now, shown in place of the label while this block is live. */
		livePhrase?: string;
		/** Between tools: the phrase is the last tool's. */
		liveDimmed?: boolean;
		load?: RunLoad;
		onToggle: () => void;
		onLoadRun?: (stub: RunStub) => void;
		row: Snippet<[TranscriptMessageEntry]>;
		pause: Snippet<[PauseAnnotation, boolean]>;
		/** A wake's delivery caption (cancelled, held), which its line would otherwise hide. */
		warning: Snippet<[TranscriptMessage]>;
		/** The running turn's tool activity, last in an open live block. */
		liveActivity?: Snippet;
		/** Plan-limit pause notes for a stub run, shown open or closed. */
		notes?: Snippet;
	} = $props();

	let section: HTMLElement | undefined = $state();
	let toggleButton: HTMLButtonElement | undefined = $state();
	// The details stay mounted while they close, so their exit is seen.
	let detailsPresent = $state(false);

	// A stub-backed block reads newest rows first: its rows only start at its start once the read is whole.
	const complete = $derived(!entry.stub || (load !== undefined && !load.loading && !load.error && !runHasMore(entry.stub, load)));
	const entries = $derived(expanded || detailsPresent ? blockEntries(entry, { complete }) : []);
	const indexById = $derived(new Map(entry.items.map((item, index) => [item.id, index])));
	const plainLabel = $derived(label.endsWith(NEW_SUFFIX) ? label.slice(0, -NEW_SUFFIX.length) : label);
	// A new phrase waits out the current one's minimum hold, then crossfades in, as the plan row does.
	const swap = createPhraseSwap(() => livePhrase);
	const liveText = $derived(liveLabel(liveTime, swap.shown));
	const ariaLabel = $derived(entry.live
		? `${expanded ? 'Collapse' : 'Expand'} current work: ${livePhrase}${liveTime ? ` · ${liveTime}` : ''}`
		: runToggleName(entry, label, expanded));

	/** The pinned header sits at its `top` offset; a collapse from there must leave the capsule there. */
	function isPinned(): boolean {
		if (!section || !toggleButton) return false;
		const top = Number.parseFloat(getComputedStyle(toggleButton).top);
		return Number.isFinite(top) && toggleButton.getBoundingClientRect().top <= top + (section.closest('.messages')?.getBoundingClientRect().top ?? 0) + 1
			&& section.getBoundingClientRect().top < toggleButton.getBoundingClientRect().top;
	}

	async function toggle(): Promise<void> {
		const keepAnchor = expanded && isPinned();
		onToggle();
		if (!keepAnchor) return;
		await tick();
		section?.scrollIntoView({ block: 'start' });
	}
</script>

<section class="transcript-fold" class:live-work={entry.live} class:expanded bind:this={section} data-work-block data-new={entry.new ? '' : undefined}>
	<button
		type="button"
		class="fold-toggle"
		use:press
		bind:this={toggleButton}
		aria-expanded={expanded}
		aria-controls={expanded ? bodyId : undefined}
		aria-label={ariaLabel}
		onclick={() => { void toggle(); }}
	>
		{#if entry.live}
			<svg class="live-spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
			<span class="live-step" class:dimmed={liveDimmed} class:fading={swap.fading} style="--fade-ms: {FADE_MS}ms" title={liveText}>{liveText}</span>
		{:else}
			<span class="fold-label">{plainLabel}</span>
			{#if entry.new}<span class="fold-new" data-new-capsule>· <i aria-hidden="true"></i>new</span>{/if}
		{/if}
		<svg class="fold-chevron" class:expanded width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
	</button>
	<Expand open={expanded} bind:present={detailsPresent}>
		<div class="fold-details" id={bodyId}>
			{#if entry.stub}
				{@const stub = entry.stub}
				{#if load?.error}
					<div class="fold-state fold-error" role="alert">
						<span>Couldn't load this work.</span>
						<button type="button" class="fold-action" onclick={() => onLoadRun?.(stub)}>Retry</button>
					</div>
				{:else if !load || load.loading}
					<div class="fold-state" role="status">Loading…</div>
				{:else if runHasMore(stub, load)}
					<button type="button" class="fold-action" onclick={() => onLoadRun?.(stub)}>Show earlier work</button>
				{/if}
			{/if}
			{#each entries as item (item.id)}
				{#if item.kind === 'step'}
					{@render row(item.item)}
				{:else if item.kind === 'line'}
					<BlockLine line={item.line} at={item.at} messageId={item.message?.id} />
					{#if item.message?.metadata?.event_disposition === 'wake'}{@render warning(item.message)}{/if}
				{:else}
					<VisitDivider truncated={item.truncated} />
				{/if}
				{#each entry.pauses?.filter((annotation) => item.kind !== 'divider' && annotation.afterItem === indexById.get(item.id)) ?? [] as annotation (annotation.pause.stoppedTurnId)}
					{@render pause(annotation, annotation.showProxy)}
				{/each}
			{/each}
			{#if entry.live}{@render liveActivity?.()}{/if}
		</div>
	</Expand>
	<!-- Held back while the details close, so a pause is never on screen twice. -->
	{#if !expanded && !detailsPresent && entry.pauses?.length}
		<div class="fold-history" aria-label="Earlier pauses">
			{#each entry.pauses as annotation (annotation.pause.stoppedTurnId)}
				{@render pause(annotation, true)}
			{/each}
		</div>
	{/if}
	{@render notes?.()}
</section>

<style>
	.transcript-fold { max-width: 760px; margin: 0 auto 14px; scroll-margin-top: var(--sticky-top, 0px); }
	/* Collapsed: a glass capsule sized to its label. Expanded: the same capsule pins as the header. */
	.fold-toggle {
		position: sticky; top: var(--sticky-top, 0px); z-index: 4;
		display: flex; width: fit-content; max-width: 100%; min-width: 0; min-height: 32px; align-items: center; gap: 8px;
		padding: 0 10px 0 12px; border: 0; border-radius: var(--radius-pill);
		background: var(--glass-thin); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-rim), 0 0 0 1px var(--border-subtle);
		color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); text-align: left; cursor: pointer;
	}
	.expanded > .fold-toggle { background: var(--glass-regular); box-shadow: var(--glass-elevation-1); }
	.fold-toggle:hover { color: var(--text-primary); }
	.fold-label, .live-step { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.fold-new { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 5px; white-space: nowrap; }
	.fold-new i { width: 6px; height: 6px; border-radius: var(--radius-circle); background: var(--accent); }
	.fold-chevron { flex: 0 0 auto; color: var(--text-tertiary); transition: transform calc(var(--dur-standard) * 0.7) var(--ease-snap); }
	.fold-chevron.expanded { transform: rotate(90deg); transition: transform var(--dur-standard) var(--ease-entrance); }
	.live-step { transition: opacity var(--fade-ms, 150ms) ease; }
	.live-step.dimmed { opacity: 0.7; }
	.live-step.fading { opacity: 0; }
	@media (prefers-reduced-motion: reduce) { .live-step { transition: none; } }
	.live-spinner { flex-shrink: 0; color: var(--accent); animation: live-spin 1s linear infinite; }
	@keyframes live-spin { to { transform: rotate(360deg); } }
	/* No box: a 2 px rule down the left edge shows where the block starts and ends. */
	.fold-details { margin: 8px 0 0 5px; padding: 2px 0 0 16px; border-left: 2px solid var(--border-subtle); }
	.fold-details :global(.message) { margin-bottom: 12px; }
	.fold-state { display: flex; align-items: center; gap: 8px; padding: 0 0 12px; color: var(--text-muted); font-size: var(--text-footnote); }
	.fold-error { color: var(--red-text); }
	.fold-error .fold-action { min-height: 32px; padding: 0 4px; }
	.fold-action { padding: 0 0 12px; border: 0; background: transparent; color: var(--accent); font: inherit; font-size: var(--text-footnote); cursor: pointer; }
	.fold-history { padding: 8px 0 0 21px; }
	.fold-details :global(.plan-limit-paused), .fold-history :global(.plan-limit-paused) { margin: 0 auto 12px; }
	@media (pointer: coarse) {
		.fold-toggle { min-height: 44px; }
	}
</style>
