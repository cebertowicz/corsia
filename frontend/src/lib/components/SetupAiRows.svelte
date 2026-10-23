<script lang="ts" module>
	export type AiRow = {
		key: string; name: string; meta: string;
		pill?: { state: 'success' | 'loading'; label: string };
		busy?: boolean; error?: boolean; detail?: string; reference?: string;
		/** The spinner stays beside the action (an install under way, with Cancel) instead of giving way to it. */
		spinning?: boolean;
		/** This ready local model is the only configured way to continue. */
		localOnly?: boolean;
		action?: { label: string; run: () => void; loading?: boolean };
		confirm?: { label: string; run: () => void }[];
		more?: { label: string; run: () => void }[];
		/** The row's own recovery copy offers a report; the setup screen wires the shared action. */
		reportable?: boolean;
		/** On or off for this source; a held switch cannot change and its detail line says why. */
		toggle?: { on: boolean; held?: boolean; run: (next: boolean) => void };
	};
</script>

<script lang="ts">
	import { fade } from 'svelte/transition';
	import { Button, ListRow, Spinner, StatePill, Toggle } from '$lib/components/ui';
	import { LOCAL_ONLY_MODEL_GUIDANCE } from '$lib/setupLocalPresentation';

	const { rows }: { rows: AiRow[] } = $props();

	function disclosureMotion(node: Element, _params: undefined, { direction }: { direction: 'in' | 'out' | 'both' }) {
		const style = getComputedStyle(node);
		const tight = Number.parseFloat(style.getPropertyValue('--dur-tight')) || 0;
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return fade(node, { duration: tight });
		const duration = Number.parseFloat(style.getPropertyValue('--dur-standard')) || 0;
		return {
			duration: direction === 'out' ? duration * 0.7 : duration,
			css: (t: number) => `grid-template-rows: ${t}fr; opacity: ${t}`,
		};
	}
</script>

<div class="rows">
	{#each rows as row (row.key)}
		<section class="item" aria-label={row.name}>
			<ListRow name={row.name} meta={[row.meta]} centered>
				{#snippet actions()}
					{#if row.action}{#if row.spinning}<Spinner size={14} />{/if}<Button variant="chip" size="sm" loading={row.action.loading} disabled={row.action.loading} onclick={row.action.run}>{row.action.label}</Button>
					{:else if row.pill}<StatePill look="dot" state={row.pill.state} label={row.pill.label} />
					{:else if row.busy}<Spinner size={14} />{/if}
					{#if row.toggle}<Toggle size="sm" checked={row.toggle.on} held={row.toggle.held} ariaLabel={row.name} describedBy={row.detail ? `setup-row-detail-${row.key}` : undefined} onchange={row.toggle.run} />{/if}
				{/snippet}
			</ListRow>
			{#if row.detail || row.reference || row.localOnly}<div class="disclosure" in:disclosureMotion out:disclosureMotion>{#if row.detail}<p class="detail" class:err={row.error} id="setup-row-detail-{row.key}">{row.detail}</p>{/if}{#if row.localOnly}<p class="detail">{LOCAL_ONLY_MODEL_GUIDANCE}</p>{/if}{#if row.reference}<p class="reference">Reference: {row.reference}</p>{/if}</div>{/if}
			{#if row.confirm ?? row.more}
				<div class="disclosure" in:disclosureMotion out:disclosureMotion><div class="inrow">{#each row.confirm ?? row.more ?? [] as item (item.label)}<Button variant="chip" size="sm" onclick={item.run}>{item.label}</Button>{/each}</div></div>
			{/if}
		</section>
	{/each}
</div>

<style>
	/* Rows keep the narrow window's width in a wide one, so an action stays beside its text. */
	.rows { display: flex; flex-direction: column; gap: var(--space-4); width: 100%; max-width: calc(var(--space-8) * 10); }
	.item :global(.row-actions) { width: calc(var(--space-8) * 3); justify-content: flex-end; }
	.disclosure { display: grid; grid-template-rows: 1fr; overflow: hidden; }
	.detail { margin: var(--space-2) 0 0; font-size: var(--text-subhead); color: var(--text-secondary); line-height: var(--lh-normal); text-wrap: pretty; }
	.detail.err { color: var(--red-text); }
	.reference { margin: var(--space-1) 0 0; font-size: var(--text-subhead); color: var(--text-secondary); }
	.inrow { display: flex; gap: var(--space-2); margin: var(--space-1) 0 0 calc(var(--space-2) * -1); }
</style>
