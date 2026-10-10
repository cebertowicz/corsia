<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';

	/**
	 * One line under the model picker's Local group while the starter model downloads or its
	 * setup failed. Try again runs the same action as the first-run card's.
	 */
	let { line, retry, onretry }: { line: string; retry: 'starter' | 'setup' | null; onretry: (kind: 'starter' | 'setup') => Promise<void> | void } = $props();

	let working = $state(false);
	async function run() {
		if (working) return;
		working = true;
		try { if (retry) await onretry(retry); } finally { working = false; }
	}
</script>

<div class="local-status">
	<span class="text" aria-live="polite">{line}</span>
	{#if retry !== null}<Button size="sm" loading={working} onclick={run}>Try again</Button>{/if}
</div>

<style>
	.local-status { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 10px 8px 32px; font-size: var(--text-caption); color: var(--text-tertiary); }
	.text { min-width: 0; overflow-wrap: anywhere; }
</style>
