<script lang="ts">
	import { tick, type Snippet } from 'svelte';
	import VisitDivider from './VisitDivider.svelte';
	import BlockLine from './BlockLine.svelte';
	import WaitCapsule from './WaitCapsule.svelte';
	import BrowserChip from './BrowserChip.svelte';
	import type { WaitFace } from './waitFace';
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
		liveOwnThread = true,
		liveActorName = null,
		livePreviewFromDescendant = false,
		load,
		onToggle,
		onLoadRun,
		row,
		pause,
		warning,
		liveActivity,
		notes,
		wait = null,
		waitThreadId = null,
		threadId = null,
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
		/** Only the visible thread's own running turn uses the accent spinner. */
		liveOwnThread?: boolean;
		/** Named when the live phrase belongs to an active descendant worker. */
		liveActorName?: string | null;
		/** The phrase is a descendant's; wait for its subscribed title before showing it. */
		livePreviewFromDescendant?: boolean;
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
		/** The plan waits on something outside and nothing runs: the chip says so in place of its settled face (EW8). */
		wait?: WaitFace | null;
		waitThreadId?: string | null;
		/** The thread whose shared browser the block's chip reopens. */
		threadId?: string | null;
	} = $props();

	let section: HTMLElement | undefined = $state();
	let toggleButton: HTMLElement | undefined = $state();

	// A stub-backed block reads newest rows first: its rows only start at its start once the read is whole.
	const complete = $derived(!entry.stub || (load !== undefined && !load.loading && !load.error && !runHasMore(entry.stub, load)));
	const entries = $derived(expanded ? blockEntries(entry, { complete }) : []);
	const indexById = $derived(new Map(entry.items.map((item, index) => [item.id, index])));
	const plainLabel = $derived(label.endsWith(NEW_SUFFIX) ? label.slice(0, -NEW_SUFFIX.length) : label);
	const hideUnidentifiedDescendant = $derived(livePreviewFromDescendant && !liveActorName);
	// A new phrase waits out the current one's minimum hold, then crossfades in, as the plan row does.
	// The actor swaps with its phrase, so a held phrase is never credited to the next actor.
	const swap = createPhraseSwap(() => JSON.stringify(hideUnidentifiedDescendant ? [null, UNKNOWN_STEP] : [liveActorName, livePhrase]));
	const shown = $derived(JSON.parse(swap.shown) as [string | null, string]);
	const shownActor = $derived(shown[0]);
	// The lead stays `Working · 3 min`; an unknown or unattributed step adds nothing after it.
	const shownPhrase = $derived(shown[1] === UNKNOWN_STEP ? null : shown[1]);
	const liveLead = $derived(liveLabel(liveTime, null));
	const liveText = $derived(liveLabel(liveTime, shownPhrase && shownActor ? `${shownActor}: ${shownPhrase}` : shownPhrase));
	const ariaLabel = $derived.by(() => {
		if (wait) return `${expanded ? 'Collapse' : 'Expand'} run. ${wait.title}`;
		if (!entry.live) return runToggleName(entry, label, expanded);
		const phrase = hideUnidentifiedDescendant ? UNKNOWN_STEP : `${liveActorName ? `${liveActorName}: ` : ''}${livePhrase}`;
		return `${expanded ? 'Collapse' : 'Expand'} current work: ${phrase}${liveTime ? ` · ${liveTime}` : ''}`;
	});

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

<section class="transcript-fold" class:live-work={entry.live || wait !== null} class:expanded bind:this={section} data-work-block data-new={entry.new ? '' : undefined}>
	{#if wait}
		<WaitCapsule
			face={wait}
			threadId={waitThreadId}
			toggle={{ expanded, controls: expanded ? bodyId : undefined, name: ariaLabel, onToggle: () => { void toggle(); } }}
			bind:element={toggleButton}
		/>
	{:else}
		<button
			type="button"
			class="fold-toggle"
			bind:this={toggleButton}
			aria-expanded={expanded}
			aria-controls={expanded ? bodyId : undefined}
			aria-label={ariaLabel}
			onclick={() => { void toggle(); }}
		>
			{#if entry.live}
				<svg class="live-spinner" class:subagent-only={!liveOwnThread} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
				<span class="live-step" class:dimmed={liveDimmed} class:fading={swap.fading} style="--fade-ms: {FADE_MS}ms" title={liveText}>{liveLead}{#if shownPhrase}{' · '}{#if shownActor}<span class="live-actor">{shownActor}:</span>{' '}{/if}{shownPhrase}{/if}</span>
			{:else}
				<span class="fold-label">{plainLabel}</span>
				{#if entry.new}<span class="fold-new" data-new-capsule><span class="fold-new-sep">·</span> <i aria-hidden="true"></i><span class="fold-new-word">new</span></span>{/if}
			{/if}
			<svg class="fold-chevron" class:expanded width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
		</button>
	{/if}
	<!-- Collapsed, the block stands for the Step that opened the browser; expanded, that Step's row shows the chip itself. -->
	{#if !expanded && entry.browserChip && threadId}
		<span class="fold-browser"><BrowserChip chip={entry.browserChip} {threadId} /></span>
	{/if}
	{#if expanded}
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
	{:else}
		{#if entry.pauses?.length}
			<div class="fold-history" aria-label="Earlier pauses">
				{#each entry.pauses as annotation (annotation.pause.stoppedTurnId)}
					{@render pause(annotation, true)}
				{/each}
			</div>
		{/if}
	{/if}
	{@render notes?.()}
</section>

<style>
	.transcript-fold { display: flex; flex-wrap: wrap; max-width: 760px; margin: 0 auto 14px; min-width: 0; align-items: center; scroll-margin-top: var(--sticky-top, 0px); }
	/* Notes (plan-limit pauses) are rows under the block, never items beside the chip. */
	.transcript-fold > :global(.plan-limit-paused) { flex: 0 0 100%; min-width: 0; }
	/* Collapsed: a glass capsule sized to its label. Expanded: the same capsule pins as the header. */
	.fold-toggle {
		position: sticky; top: var(--sticky-top, 0px); z-index: 4;
		display: flex; width: fit-content; max-width: 100%; min-width: 0; min-height: 32px; align-items: center; gap: 8px;
		padding: 0 10px 0 12px; border: 0; border-radius: var(--radius-pill);
		background: var(--glass-thin); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-rim), 0 0 0 1px var(--border-subtle);
		color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); text-align: left; cursor: pointer;
	}
	.expanded .fold-toggle { background: var(--glass-regular); box-shadow: var(--glass-elevation-1); }
	.fold-toggle:hover { color: var(--text-primary); }
	.fold-label, .live-step { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.live-actor { color: var(--text-secondary); }
	.fold-new { display: inline-flex; flex: 0 0 auto; align-items: center; gap: var(--space-1); margin-left: calc(-1 * var(--space-1)); white-space: nowrap; }
	.fold-new i { width: calc(var(--space-1) * 1.5); height: calc(var(--space-1) * 1.5); border-radius: var(--radius-circle); background: var(--accent); }
	.fold-new-word { color: var(--accent-text); }
	/* min-width 0: a flex item's automatic minimum is its content's min-content, so one nowrap tool phrase would widen the pane past the phone. */
	.fold-details, .fold-history { flex: 0 0 100%; min-width: 0; box-sizing: border-box; }
	.fold-browser { display: inline-flex; min-width: 0; margin-left: var(--space-2); }
	.fold-chevron { flex: 0 0 auto; color: var(--text-tertiary); transition: transform var(--dur-tight); }
	.fold-chevron.expanded { transform: rotate(90deg); }
	.live-step { transition: opacity var(--fade-ms, 150ms) ease; }
	.live-step.dimmed { opacity: 0.7; }
	.live-step.fading { opacity: 0; }
	@media (prefers-reduced-motion: reduce) { .live-step { transition: none; } }
	.live-spinner { flex-shrink: 0; color: var(--status-in-progress); animation: live-spin 1s linear infinite; }
	.live-spinner.subagent-only { color: var(--text-tertiary); }
	@keyframes live-spin { to { transform: rotate(360deg); } }
	/* No box: a 2 px rule down the left edge shows where the block starts and ends. */
	.fold-details { margin: 8px 0 0 5px; padding: 2px 0 0 16px; border-left: 2px solid var(--border-subtle); }
	.fold-details :global(.message) { margin-bottom: 12px; }
	.fold-state { display: flex; align-items: center; gap: 8px; padding: 0 0 12px; color: var(--text-muted); font-size: var(--text-footnote); }
	.fold-error { color: var(--red-text); }
	.fold-error .fold-action { min-height: 32px; padding: 0 4px; }
	.fold-action { padding: 0 0 12px; border: 0; background: transparent; color: var(--accent-text); font: inherit; font-size: var(--text-footnote); cursor: pointer; }
	.fold-history { padding: 8px 0 0 21px; }
	.fold-details :global(.plan-limit-paused), .fold-history :global(.plan-limit-paused) { margin: 0 auto 12px; }
	@media (pointer: coarse) {
		.fold-toggle { min-height: 44px; }
	}
</style>
