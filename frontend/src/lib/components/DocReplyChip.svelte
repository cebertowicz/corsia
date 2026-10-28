<script lang="ts">
	import { get } from 'svelte/store';
	import { fade, fly } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { ms } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import UnreadReplyChip from './comments/UnreadReplyChip.svelte';

	// Floats over the document where its "N new above / below" pill does (DocCanvasActivity), in the editor's grid cell so it
	// follows the comments drawer. The chip is the chat's own (UnreadReplyChip); only where it sits and where a tap goes are the document's.
	let { artifactId, path, threadId, count, onStep }: {
		artifactId: string;
		path: string;
		threadId: string | null;
		count: number;
		onStep: () => void;
	} = $props();

	// Arrives where it lands (fade, 4px rise) and leaves by fading at 70%; reduced motion crossfades only (motion.md).
	function arrive(node: HTMLElement) {
		const reduced = get(prefersReducedMotion);
		return fly(node, { y: reduced ? 0 : 4, duration: ms(reduced ? '--dur-tight' : '--dur-standard'), easing: cubicOut });
	}
	function leave(node: HTMLElement) {
		return fade(node, { duration: ms('--dur-standard') * 0.7 });
	}
</script>

{#if count > 0}
	<div class="doc-reply-chip" data-doc-reply-chip in:arrive out:leave>
		<UnreadReplyChip {artifactId} {path} {threadId} {count} onOpen={onStep} />
	</div>
{/if}

<style>
	/* The editor's cell, bottom-left: the activity pill takes the right edge, the delivery confirmation the middle. */
	.doc-reply-chip {
		grid-area: editor;
		align-self: end;
		justify-self: start;
		z-index: 8;
		min-width: 0;
		max-width: calc(100% - 2 * var(--space-3));
		margin: 0 var(--space-3) var(--space-3);
		border-radius: var(--radius-pill);
		background: var(--bg-primary);
		box-shadow: var(--shadow-chip);
	}
</style>
