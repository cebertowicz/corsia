<script lang="ts">
	import { get } from 'svelte/store';
	import { fade, fly } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { ms } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import UnreadReplyChip from './comments/UnreadReplyChip.svelte';
	import { unreadReplyCount } from '$lib/docReplyChip';
	import type { DocComment } from '$lib/stores/docComments';

	// The document's notice uses the chat's attachment chip and New marker; its caller supplies the destination.
	let { artifactId, path, threadId, threads, onStep }: {
		artifactId: string;
		path: string;
		threadId: string | null;
		threads: DocComment[];
		onStep: () => void;
	} = $props();
	const count = $derived(unreadReplyCount(threads));

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
	<div class="doc-reply-chip" data-doc-reply-chip in:arrive|global out:leave|global>
		<UnreadReplyChip {artifactId} {path} {threadId} {count} onOpen={onStep} />
	</div>
{/if}

<style>
	.doc-reply-chip { display: flex; align-items: center; min-width: 0; min-height: var(--touch-target); max-width: 100%; }
	.doc-reply-chip :global(button.tile.chip) { box-shadow: var(--shadow-chip); }
</style>
