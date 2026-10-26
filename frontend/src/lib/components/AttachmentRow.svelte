<script lang="ts">
	// One artifact attached to the transcript: a flat row (an ask, or a blocked update's ask) or a chip
	// (a file the message names). The whole element opens the artifact in the canvas; it has no answer
	// buttons, because the review bar in the opened document is the one place to answer an ask.
	import { CARD_STATE_LABELS, feedbackLine, type MessageCard, type MessageCardState } from '$lib/api/messageCards';
	import { imageContentUrl } from '$lib/artifactImage';
	import { token } from '$lib/stores/auth';
	import { openArtifactTab } from '$lib/stores/artifactWorkspace';
	import { uiScope } from '$lib/stores/uiScope';
	import { isPaneDrawer, paneOpen } from '$lib/stores/paneOpen';
	import { submittedAnswers } from '$lib/stores/cardAnswers';
	import ArtifactGlyph from './ArtifactGlyph.svelte';
	import NeedsYouTile from './NeedsYouTile.svelte';
	import OutcomeTime from './OutcomeTime.svelte';
	import NewMarker from './comments/NewMarker.svelte';

	let {
		card,
		threadId,
		variant = 'row',
		chipWidth = 'default',
		newMarker = false,
		task,
		time,
	}: {
		card: MessageCard;
		threadId: string | null | undefined;
		/** `row`: tile, title and a context line. `chip`: tile and title only. */
		variant?: 'row' | 'chip';
		/** Opt-in for a chip whose title is its only location cue; ordinary attachment chips retain 220px. */
		chipWidth?: 'default' | 'content';
		/** Show the shared unread marker on a chip. */
		newMarker?: boolean;
		/** The plan item whose block this ask answers; a context-line segment that truncates before the state. */
		task?: string;
		/** When the block happened; read through the transcript's own short time. */
		time?: string;
	} = $props();

	// A review-bar submission shows at once and stays for this session because cards are not refetched; it belongs
	// to this Plans-owned hold only, so a later ask for the same artifact remains open.
	const sent = $derived(card.state === 'needs_review' && card.hold_id ? $submittedAnswers[card.hold_id] : undefined);
	const cardState = $derived(sent?.state ?? card.state);
	const feedback = $derived(sent ? sent.feedback : card.feedback);
	// A row's tile is a filled type tile; a chip carries the bare coloured glyph, the pill being its container.
	const TILE = { row: 36, chip: 24 } as const;
	const stateLabel = $derived(
		cardState ? CARD_STATE_LABELS[cardState] : card.available ? null : CARD_STATE_LABELS.unavailable,
	);
	// Only an open ask asks anything of you; a verdict is history and a replaced or gone ask is muted.
	const tone = $derived.by((): 'ask' | 'verdict' | 'muted' | null => {
		const state: MessageCardState | undefined = cardState ?? (card.available ? undefined : 'unavailable');
		if (!state) return null;
		if (state === 'needs_review') return 'ask';
		return state === 'closed' || state === 'replaced' || state === 'withdrawn' || state === 'unavailable' ? 'muted' : 'verdict';
	});
	// A verdict that carries feedback shows its counts in the task's place (C3); otherwise the task, then the file
	// name, which repeats the title for a file titled by its name: omit it, comparing the full strings.
	const counts = $derived(
		cardState === 'approved_with_changes' || cardState === 'changes_requested' ? feedbackLine(feedback) : null,
	);
	const detail = $derived(
		counts ?? task ?? (card.name && card.name !== card.title ? card.name : null),
	);
	// Whether a file is an image is the backend's answer alone: its artifact type, as the canvas reads it.
	// A thumbnail that fails to load falls back to the glyph.
	let broken = $state(false);
	const thumbSrc = $derived(
		card.type === 'image' && card.available && card.path && !broken
			? imageContentUrl({ path: card.path, projectId: $uiScope?.id ?? null, token: $token ?? '' })
			: null,
	);
	const typeWord = $derived((card.type ?? 'file').replace(/-/g, ' '));
	const accessibleName = $derived([card.title, typeWord, stateLabel, counts].filter(Boolean).join(', '));
	const interactive = $derived(Boolean(card.opens && card.available && threadId && card.path));

	function open(event: MouseEvent): void {
		// The chat column marks chat the active surface on any click inside it; bubbling there would
		// flip the artifact just opened back to chat, which on a narrow canvas hides it.
		event.stopPropagation();
		if (!threadId || !card.path) return;
		openArtifactTab(threadId, { artifactId: card.artifact_id, path: card.path, title: card.name ?? card.title });
		// The pane is a drawer on narrow canvases; leaving it open would cover the document.
		if (isPaneDrawer()) paneOpen.set(false);
	}
</script>

{#snippet glyph()}
	{#if thumbSrc}
		<img
			class="thumb"
			style="--tile: {TILE[variant]}px"
			src={thumbSrc}
			alt=""
			loading="lazy"
			onerror={() => (broken = true)}
		/>
	{:else}
		<ArtifactGlyph icon={card.icon} tile={variant === 'row' ? TILE.row : 0} />
	{/if}
{/snippet}
{#snippet clock()}<OutcomeTime value={time ?? ''} compact />{/snippet}
{#snippet stateLine()}
	{#if stateLabel}
		{#key cardState}
			<span class="state {tone}">
				{#if tone === 'ask'}
					<span class="dot" aria-hidden="true"></span>
				{:else if cardState === 'changes_requested'}
					<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></svg>
				{:else if tone === 'verdict'}
					<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
				{/if}{stateLabel}
			</span>
		{/key}
	{/if}
	{#if detail}<span class="rest">{#if stateLabel}<span class="sep" aria-hidden="true">·</span>{/if}<span class="name">{detail}</span></span>{/if}
{/snippet}
{#snippet chevronMark()}
	{#if interactive}
		<svg class="chevron" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
	{:else}
		<!-- No chevron, so keep its width: the time stays in the same column on every row. -->
		<span class="chevron" aria-hidden="true"></span>
	{/if}
{/snippet}
{#snippet chipFace()}
	{@render glyph()}
	<span class="text"><span class="head"><span class="title">{card.title}</span>{#if newMarker}<NewMarker />{/if}</span></span>
{/snippet}

{#if variant === 'row'}
	<NeedsYouTile
		as={interactive ? 'button' : 'div'}
		needsYou={interactive && tone === 'ask'}
		tone={interactive ? null : !card.available ? 'gone' : tone === 'muted' ? 'muted' : null}
		counted={Boolean(counts)}
		title={card.title}
		lead={glyph}
		aside={time ? clock : undefined}
		ctx={stateLabel || detail ? stateLine : undefined}
		trailing={chevronMark}
		data-artifact-id={card.artifact_id}
		data-card-kind={card.kind}
		data-card-state={cardState ?? ''}
		aria-label={accessibleName}
		role={interactive ? undefined : 'group'}
		onclick={interactive ? open : undefined}
	/>
{:else if interactive}
	<button
		type="button"
		class="tile chip press-tint"
		class:thumbed={Boolean(thumbSrc)}
		class:needs-you={tone === 'ask'}
		class:chip-content-width={chipWidth === 'content'}
		data-artifact-id={card.artifact_id}
		data-card-kind={card.kind}
		data-card-state={cardState ?? ''}
		title={card.title}
		aria-label={accessibleName}
		onclick={open}
	>{@render chipFace()}</button>
{:else}
	<div
		class="tile chip static"
		class:thumbed={Boolean(thumbSrc)}
		class:gone={!card.available}
		class:muted={tone === 'muted'}
		class:chip-content-width={chipWidth === 'content'}
		data-artifact-id={card.artifact_id}
		data-card-kind={card.kind}
		data-card-state={cardState ?? ''}
		title={card.title}
		role="group"
		aria-label={accessibleName}
	>{@render chipFace()}</div>
{/if}

<style>
	.tile {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		min-width: 0;
		border: 0;
		background: transparent;
		color: var(--text-primary);
		font: inherit;
		text-align: left;
		transition: background var(--dur-tight) var(--ease-snap);
	}
	/* A chip is the row condensed, in the token chip's geometry without its remove mark: a pill holding the bare coloured
	   glyph and the title, filled so it reads as a thing and not as prose. 32 px tall; on touch 36 px (DS D31), the
	   whole chip being the hit area. A thumbnail keeps its own small image as the leading mark. */
	.tile.chip {
		display: inline-flex;
		gap: var(--space-2);
		max-width: 220px;
		min-height: 32px;
		padding: 0 var(--space-3);
		border-radius: var(--radius-pill);
		background: var(--bg-tertiary);
	}
	.tile.chip.thumbed { padding-left: var(--space-1); }
	/* Reply notices use the available chat width for the document title. */
	.tile.chip.chip-content-width { max-width: 100%; }
	button.tile { cursor: pointer; }
	@media (hover: hover) {
		button.tile.chip:hover { background: var(--bg-hover); }
	}
	/* Inset: the message clips at the prose edge and the phone's chip strip scrolls, so an outward ring would be cut off. */
	button.tile:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (pointer: coarse) {
		.tile.chip { min-height: 36px; }
	}
	.thumb {
		flex: 0 0 auto;
		width: var(--tile);
		height: var(--tile);
		border-radius: var(--radius-sm);
		object-fit: cover;
		background: var(--bg-tertiary);
		outline: 1px solid var(--border-subtle);
		outline-offset: -1px;
	}
	.tile.chip .thumb { border-radius: var(--radius-xs); }
	.text { display: flex; flex-direction: column; flex: 1 1 auto; min-width: 0; }
	.tile.chip .text { flex: 0 1 auto; }
	.title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--text-callout);
		font-weight: var(--fw-semibold);
		line-height: var(--lh-tight);
	}
	.tile.chip .title { font-size: var(--text-footnote); font-weight: var(--fw-medium); }
	/* The title and the time share one line, the time on the title's baseline; only the title gives way. */
	.head { display: flex; align-items: baseline; gap: var(--space-2); min-width: 0; }
	.head .title { flex: 1 1 auto; min-width: 0; }
	/* The state never truncates; the task (or the feedback counts) gives way first. */
	.sep { flex: 0 0 auto; }
	.rest { display: flex; align-items: center; gap: var(--space-1); flex: 0 1 auto; min-width: 0; }
	.name { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	:global(.ctx.counted) .rest { flex: 0 0 auto; }
	:global(.ctx.counted) .name { overflow: visible; }
	.state { animation: state-in var(--dur-tight) var(--ease-snap); }
	.state.muted { color: var(--text-tertiary); }
	.sep { color: var(--text-tertiary); }
	.chevron { flex: 0 0 auto; width: 16px; color: var(--text-tertiary); }
	.tile.gone .title, .tile.muted .title { color: var(--text-secondary); }
	.tile.gone .title { color: var(--text-tertiary); }
	@keyframes state-in { from { opacity: 0; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) {
		.state { animation: none; }
		.tile { transition: none; }
	}
</style>
