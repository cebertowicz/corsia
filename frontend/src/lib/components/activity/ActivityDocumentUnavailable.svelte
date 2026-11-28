<script lang="ts">
	import type { Snippet } from 'svelte';
	import { Button } from '$lib/components/ui';
	import ActivityKindPanel from './ActivityKindPanel.svelte';

	let { label, failed, sourceThread, threadLabel, notice, onOpenChat }: {
		label: string;
		failed: boolean;
		sourceThread: string | null;
		threadLabel: string;
		notice?: Snippet;
		onOpenChat: (id: string) => void;
	} = $props();
</script>

<ActivityKindPanel {notice} {label}>
	{#snippet body()}
		<div class="unopenable" role="status">
			<h1>Couldn’t open this document.</h1>
			<p>{failed ? 'Its chat’s files didn’t load.' : 'It isn’t in the files of the chat it came from; it may have been moved or removed.'}</p>
			{#if sourceThread}
				<Button size="sm" variant="secondary" onclick={() => onOpenChat(sourceThread)}>Open {threadLabel}’s chat</Button>
			{/if}
		</div>
	{/snippet}
</ActivityKindPanel>

<style>
	.unopenable { padding-top: var(--space-4); }
	h1 { margin: 0 0 var(--space-3); font-size: var(--text-title3); line-height: 1.35; color: var(--text-primary); }
	p { margin: 0 0 var(--space-3); color: var(--text-secondary); font-size: var(--text-body); line-height: var(--lh-relaxed); }
	:global(.activity.phone) p { font-size: var(--text-input); }
</style>
