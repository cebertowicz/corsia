<script lang="ts">
	// The image viewer's comment controls (DCOL-FR13.3, FR13.6): one Comment
	// button that enters box mode, and the comments pill with its count, which
	// opens the same comments drawer as the document's pill.
	let { boxMode, ready = true, moving = false, savingMove = false, count, loading = false, listOpen = false, readOnly = false, onToggle, onOpenList }: {
		boxMode: boolean;
		/** The image has loaded, so there is something to draw on. */
		ready?: boolean;
		/** Box mode is placing a moved pin rather than a new comment. */
		moving?: boolean;
		/** The newly drawn pin remains visible while its move request is in flight. */
		savingMove?: boolean;
		count: number;
		loading?: boolean;
		listOpen?: boolean;
		readOnly?: boolean;
		onToggle: (opener: HTMLElement) => void;
		onOpenList: (opener: HTMLElement) => void;
	} = $props();
	const status = $derived(savingMove ? 'Moving pin…' : moving ? 'Draw the pin’s new place on the image' : boxMode ? 'Drag on the image for a box, click for a point, or press Enter for the whole image' : '');
</script>

{#if !readOnly}
	<button type="button" class="control" data-image-comment aria-pressed={boxMode} disabled={!ready} aria-busy={savingMove || undefined} onclick={(event) => onToggle(event.currentTarget)}>
		<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 2.5v7M4.5 6h7M3 13.5l2.2-2.2M13 13.5l-2.2-2.2" /></svg>
		<span>{savingMove ? 'Moving pin…' : moving ? 'Move pin' : 'Comment'}</span>
	</button>
{/if}
<button type="button" class="control pill" data-image-comments-pill aria-expanded={listOpen} disabled={loading}
	aria-label={loading ? 'Loading comments' : `${count} comment${count === 1 ? '' : 's'}`} onclick={(event) => onOpenList(event.currentTarget)}>
	<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg>
	<span class="count">{loading ? '…' : count}</span>
</button>
<span class="sr-only" role="status">{status}</span>

<style>
	/* `.control` is the viewer bar's own look, defined once by ImageLightbox. */
	.count { font-variant-numeric: tabular-nums; }
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
