<script lang="ts">
	import ThreadWaitingMark from './ThreadWaitingMark.svelte';
	import WaitFaceText from './WaitFaceText.svelte';
	import type { WaitFace } from './waitFace';

	// Non-Focus mode has no run chip: the same face sits under the last reply, where a running turn's
	// activity shows, never both. It is static: no dots, no pulse (EW8).
	let { face, threadId = null }: { face: WaitFace; threadId?: string | null } = $props();
</script>

<div class="message wait-line" role="status" aria-label={face.title}>
	<ThreadWaitingMark size={12} />
	<WaitFaceText {face} {threadId} />
</div>

<style>
	.wait-line {
		display: flex; align-items: center; gap: var(--chip-inline-padding); max-width: var(--transcript-max-width); margin: 0 auto var(--space-5);
		color: var(--text-secondary); font-size: var(--text-footnote);
	}
</style>
