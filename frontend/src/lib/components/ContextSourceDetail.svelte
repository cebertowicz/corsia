<script lang="ts">
	import type { ContextSheetSourceRow } from './contextSheetModel';

	/** The drill-down panel for one context source row. Presentational only. */
	let {
		source,
		onBack,
	}: {
		source: ContextSheetSourceRow;
		onBack: () => void;
	} = $props();
</script>

<div class="context-source-detail">
	<div class="context-source-detail-top">
		<button class="context-source-back" onclick={onBack}>Back</button>
	</div>
	<div class="context-source-kicker">Context source</div>
	<div class="context-source-title" title={source.fullLabel || source.label}>{source.label}</div>
	{#if source.fullLabel && source.fullLabel !== source.label}
		<div class="context-source-full">Full name: <span class="context-source-full-name">{source.fullLabel}</span></div>
	{/if}
	<p class="context-source-body">
		{source.statusLabel}
		{#if source.reason}
			· {source.reason}
		{:else if source.status !== 'included'}
			· No content was generated for this preview.
		{/if}
	</p>
	<div class="context-source-meta">
		<span>{source.category}</span>
		{#each source.tags ?? [] as tag (tag)}
			<span>{tag}</span>
		{/each}
		{#if source.tokenLabel}
			<span>{source.tokenLabel}</span>
		{/if}
		{#if source.ownerLabel}
			<span>{source.ownerLabel}</span>
		{/if}
	</div>
</div>

<style>
	.context-source-detail {
		display: flex;
		flex-direction: column;
		gap: 10px;
		min-width: 0;
	}
	.context-source-detail-top {
		display: flex;
		justify-content: space-between;
		gap: 8px;
	}
	.context-source-back {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-xs);
		background: var(--bg-primary);
		color: var(--text-secondary);
		cursor: pointer;
		font: inherit;
		font-size: var(--text-footnote);
		font-weight: 600;
		padding: 6px 9px;
	}
	.context-source-back:hover {
		background: var(--bg-hover);
		color: var(--text-primary);
	}
	.context-source-kicker {
		color: var(--text-secondary);
		font-size: var(--text-caption);
		font-weight: 700;
		text-transform: uppercase;
	}
	.context-source-title {
		color: var(--text-primary);
		font-size: var(--text-subhead);
		font-weight: 700;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.context-source-full {
		color: var(--text-secondary);
		font-size: var(--text-footnote);
		overflow-wrap: anywhere;
	}
	.context-source-full-name {
		color: var(--text-secondary);
		font-family: var(--font-mono);
	}
	.context-source-body {
		color: var(--text-secondary);
		font-size: var(--text-subhead);
		line-height: 1.45;
		margin: 0;
		white-space: pre-wrap;
	}
	.context-source-meta {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		color: var(--text-secondary);
		font-size: var(--text-caption);
	}
</style>
