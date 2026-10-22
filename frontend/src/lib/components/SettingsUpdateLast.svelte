<script lang="ts">
	import { Button } from '$lib/components/ui';
	import type { FailureCopy } from '$lib/update/failureCopy';
	import { restartToFinishUpdate } from '$lib/update/restartToFinish';
	import { retryUpdate, updateStarting } from '$lib/update/store';
	import type { UpdateFailure } from '$lib/update/model';

	/** The last update that did not install: the cause, what to do, and the raw reason behind Details. */
	interface Props {
		copy: FailureCopy;
		failure: UpdateFailure;
	}

	const { copy, failure }: Props = $props();

	const summary = $derived(failure.summary ? `${failure.summary.replace(/\.$/, '')}.` : null);
	const hasDetails = $derived(Boolean(summary || failure.detail || failure.log));
	function retry(): void { void (copy.retryAction === 'restart' ? restartToFinishUpdate() : retryUpdate()); }
</script>

<div class="service-row last-update">
	<div class="body">
		<span class="service-name">Last update</span>
		<span class="headline">{copy.headline}</span>
		{#if copy.body}<span class="detail">{copy.body}</span>{/if}
		{#if failure.conductorTold}<span class="detail">The CORSIA agent has been told.</span>{/if}
		{#if hasDetails}
			<details>
				<summary>Details</summary>
				{#if summary}<pre>{summary}</pre>{/if}
				{#if failure.detail}<pre>{failure.detail}</pre>{/if}
				{#if failure.log}<pre>{failure.log}</pre>{/if}
			</details>
		{/if}
	</div>
	{#if copy.retryLabel}
		<div class="service-actions"><Button variant="ghost" size="sm" loading={$updateStarting} onclick={retry}>{copy.retryLabel}</Button></div>
	{/if}
</div>

<style>
	/* A §0.1 list row shaped like SettingsRow: the name, the failure under it in the status text
	   colour (not a box), and the verb on the content edge at every width. */
	.service-row { display: flex; align-items: center; gap: var(--space-2); min-height: var(--list-row-min-height); padding: var(--space-1) var(--space-2); }
	.service-name { font-size: var(--text-subhead); color: var(--text-primary); }
	.body { flex: 1; display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; font-size: var(--text-caption); line-height: 1.45; }
	.headline { color: var(--amber-text); }
	.detail { color: var(--text-tertiary); }
	.service-actions { flex-shrink: 0; display: flex; align-items: center; gap: var(--space-1); }
	.service-actions > :global(.btn.ghost:last-child) { margin-right: calc(-1 * var(--button-pad-x-sm)); }
	summary { cursor: pointer; color: var(--accent-text); }
	pre { max-height: 180px; overflow: auto; margin: var(--space-2) 0 0; white-space: pre-wrap; color: var(--text-tertiary); font: var(--text-caption)/1.45 var(--font-mono, monospace); }
</style>
