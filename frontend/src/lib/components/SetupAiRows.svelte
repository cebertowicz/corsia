<script lang="ts" module>
	export type AiRow = {
		key: string; name: string; meta: string;
		pill?: { state: 'success' | 'loading'; label: string };
		busy?: boolean; error?: boolean; detail?: string;
		action?: { label: string; run: () => void; loading?: boolean };
		confirm?: { label: string; run: () => void }[];
		more?: { label: string; run: () => void }[];
	};
</script>

<script lang="ts">
	import { Button, ListRow, Spinner, StatePill } from '$lib/components/ui';

	const { rows }: { rows: AiRow[] } = $props();
</script>

<div class="rows">
	{#each rows as row (row.key)}
		<section class="item" aria-label={row.name}>
			<ListRow name={row.name} meta={[row.meta]} centered>
				{#snippet actions()}
					{#if row.pill}<StatePill look="dot" state={row.pill.state} label={row.pill.label} />
					{:else if row.busy && !row.action}<Spinner size={14} />
					{:else if row.action}<Button variant="chip" size="sm" loading={row.action.loading} disabled={row.action.loading} onclick={row.action.run}>{row.action.label}</Button>{/if}
				{/snippet}
			</ListRow>
			{#if row.detail}<p class="detail" class:err={row.error}>{row.detail}</p>{/if}
			{#if row.confirm ?? row.more}
				<div class="inrow">{#each row.confirm ?? row.more ?? [] as item (item.label)}<Button variant="chip" size="sm" onclick={item.run}>{item.label}</Button>{/each}</div>
			{/if}
		</section>
	{/each}
</div>

<style>
	/* Rows keep the narrow window's width in a wide one, so an action stays beside its text. */
	.rows { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 10); }
	.detail { margin: var(--space-2) 0 0; font-size: var(--text-subhead); color: var(--text-secondary); line-height: var(--lh-normal); text-wrap: pretty; }
	.detail.err { color: var(--red-text); }
	.inrow { display: flex; gap: var(--space-2); margin: var(--space-1) 0 0 calc(var(--space-2) * -1); }
</style>
