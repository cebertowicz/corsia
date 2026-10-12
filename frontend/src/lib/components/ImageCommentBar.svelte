<script lang="ts">
	// The image viewer's comment controls (DCOL-FR13.3, FR13.6): one Comment
	// button that enters box mode, and the comments pill with its count, which
	// opens the same comments drawer as the document's pill and is that pill.
	import CommentsPill from './comments/CommentsPill.svelte';

	let { boxMode, ready = true, moving = false, savingMove = false, count, unsent = 0, loading = false, listOpen = false, readOnly = false, onToggle, onOpenList }: {
		boxMode: boolean;
		/** The image has loaded, so there is something to draw on. */
		ready?: boolean;
		/** Box mode is placing a moved pin rather than a new comment. */
		moving?: boolean;
		/** The newly drawn pin remains visible while its move request is in flight. */
		savingMove?: boolean;
		/** Open threads on this image. */
		count: number;
		/** Messages in the viewer's feedback batch (a document's, for an image in one) that are not sent yet. */
		unsent?: number;
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
<CommentsPill class="control pill" data-image-comments-pill {count} {unsent} {loading} expanded={listOpen} disabled={loading} onOpen={onOpenList} />
<span class="sr-only" role="status">{status}</span>

<style>
	/* `.control` is the viewer bar's own look, defined once by ImageLightbox. */
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
