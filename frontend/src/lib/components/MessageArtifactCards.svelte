<script lang="ts">
	// The artifacts a message or a blocked update row carries, as a row of tiles.
	// A tile opens its artifact in the canvas; it has no answer buttons, because the
	// review bar in the opened document is the one place to answer an ask.
	import { CARD_STATE_LABELS, feedbackLine, type MessageCard } from '$lib/api/messageCards';
	import { openArtifactTab } from '$lib/stores/artifactWorkspace';
	import { isPaneDrawer, paneOpen } from '$lib/stores/paneOpen';
	import ArtifactGlyph from './ArtifactGlyph.svelte';

	let {
		cards,
		more = 0,
		threadId,
		hiddenHolds,
	}: {
		cards: MessageCard[];
		/** Cards past the cap; the last tile opens the artifact pane. */
		more?: number;
		threadId: string | null | undefined;
		/** Holds whose card renders on their blocked update row instead. */
		hiddenHolds?: ReadonlySet<string>;
	} = $props();

	const shown = $derived(cards.filter((card) => !(card.hold_id && hiddenHolds?.has(card.hold_id))));

	const stateLabel = (card: MessageCard): string | null =>
		card.state ? CARD_STATE_LABELS[card.state] : card.available ? null : CARD_STATE_LABELS.unavailable;
	const secondary = (card: MessageCard): string | null => feedbackLine(card.feedback) ?? card.name;
	const typeWord = (card: MessageCard): string => (card.type ?? 'file').replace(/-/g, ' ');
	const accessibleName = (card: MessageCard): string =>
		[card.title, typeWord(card), stateLabel(card)].filter(Boolean).join(', ');

	function open(card: MessageCard): void {
		if (!threadId || !card.path) return;
		openArtifactTab(threadId, { artifactId: card.artifact_id, path: card.path, title: card.name ?? card.title });
		// The pane is a drawer on narrow canvases; leaving it open would cover the document.
		if (isPaneDrawer()) paneOpen.set(false);
	}
</script>

{#snippet face(card: MessageCard)}
	<ArtifactGlyph icon={card.icon} />
	<span class="text">
		<span class="title">{card.title}</span>
		{#if stateLabel(card)}
			<span class="state" class:needs={card.state === 'needs_review'}>{stateLabel(card)}</span>
		{/if}
		{#if secondary(card)}<span class="name">{secondary(card)}</span>{/if}
	</span>
{/snippet}

{#if shown.length > 0 || more > 0}
	<ul class="cards" aria-label="Artifacts">
		{#each shown as card (card.hold_id ?? card.artifact_id)}
			<li class="slot">
				{#if card.opens && card.available && threadId && card.path}
					<button
						type="button"
						class="tile"
						data-artifact-id={card.artifact_id}
						data-card-kind={card.kind}
						data-card-state={card.state ?? ''}
						title={card.title}
						aria-label={accessibleName(card)}
						onclick={() => open(card)}
					>{@render face(card)}</button>
				{:else}
					<div
						class="tile static"
						class:gone={!card.available}
						data-artifact-id={card.artifact_id}
						data-card-kind={card.kind}
						data-card-state={card.state ?? ''}
						title={card.title}
						role="group"
						aria-label={accessibleName(card)}
					>{@render face(card)}</div>
				{/if}
			</li>
		{/each}
		{#if more > 0}
			<li class="slot">
				<button type="button" class="tile more" onclick={() => paneOpen.set(true)}>+{more} more</button>
			</li>
		{/if}
	</ul>
{/if}

<style>
	.cards {
		display: flex;
		align-items: stretch;
		gap: 8px;
		margin: 8px 0 0;
		padding: 0 0 4px;
		list-style: none;
		overflow-x: auto;
		scroll-snap-type: x proximity;
	}
	.slot { display: flex; flex: 0 0 auto; scroll-snap-align: start; }
	.tile {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 232px;
		min-width: 200px;
		padding: 8px 10px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		background: var(--bg-secondary);
		color: var(--text-primary);
		font: inherit;
		text-align: left;
	}
	button.tile { cursor: pointer; }
	button.tile:hover { background: var(--bg-hover); }
	button.tile:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
	.tile.gone { border-style: dashed; color: var(--text-tertiary); }
	.tile.more { width: auto; min-width: 0; align-self: center; font-size: var(--text-footnote); color: var(--text-secondary); }
	.text { display: flex; flex-direction: column; min-width: 0; gap: 1px; }
	.title, .name, .state {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.title { font-size: var(--text-footnote); }
	.name, .state { font-size: var(--text-caption); color: var(--text-tertiary); }
	.state { color: var(--text-secondary); }
	.state.needs { color: var(--amber); }
</style>
