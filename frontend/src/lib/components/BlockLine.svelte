<script lang="ts">
	import OutcomeTime from './OutcomeTime.svelte';
	import type { BlockLine } from './transcriptBlockEntries';

	// One quiet line inside an open work block: what happened and when.
	let { line, at, messageId }: { line: BlockLine; at: string | null; messageId?: string } = $props();
</script>

<div class="block-line" class:error={line.type === 'error_recovered'} data-block-line={line.type} data-message-id={messageId}>
	<span class="when">{#if at}<OutcomeTime value={at} compact />{/if}</span>
	<span class="text">{line.text}</span>
</div>

<style>
	.block-line { display: flex; align-items: baseline; gap: 10px; min-width: 0; padding: 4px 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.when { flex: 0 0 auto; min-width: 56px; font-size: var(--text-caption); white-space: nowrap; }
	.when :global(time) { white-space: nowrap; }
	.text { min-width: 0; overflow-wrap: anywhere; }
	.error { color: var(--red-text); }
	.error .when { color: var(--text-tertiary); }
</style>
