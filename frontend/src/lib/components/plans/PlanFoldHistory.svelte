<!-- What reading a level's older finished steps is doing: loading, failed (with Retry) or came back empty. -->
<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';
	import type { LevelHistory } from './foldHistory.svelte';

	let { history, depth, exhausted, onRetry }: {
		history: LevelHistory;
		/** The tree depth the status row renders at. */
		depth: number;
		/** The history ended with fewer rows than the server counted. */
		exhausted: boolean;
		onRetry: () => void;
	} = $props();
</script>

{#if history.status === 'loading'}
	<div class="tree-row child-row" role="status" style={`--depth:${depth}`}>Loading…</div>
{:else if history.status === 'error'}
	<div class="tree-row child-row child-error" role="alert" style={`--depth:${depth}`}>
		<span>Couldn’t load earlier finished steps.</span>
		<Button variant="ghost" size="sm" onclick={onRetry}>Retry</Button>
	</div>
{:else if history.status === 'end' && exhausted}
	<div class="tree-row child-row" role="status" style={`--depth:${depth}`}>No earlier finished steps to show.</div>
{/if}

<style>
	.tree-row {
		display: flex; flex-wrap: nowrap; align-items: center; gap: var(--space-2); min-height: 38px;
		padding: 6px 8px 6px calc(4px + var(--depth) * 22px); border-radius: var(--radius-xs); width: 100%;
	}
	.child-row { color: var(--text-secondary); }
	.child-error { flex-wrap: wrap; color: var(--red-text); justify-content: space-between; }
</style>
