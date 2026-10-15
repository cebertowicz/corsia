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
	<span class="service-name">Last update</span>
	<div class="body">
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
		<div class="service-actions"><Button size="sm" loading={$updateStarting} onclick={retry}>{copy.retryLabel}</Button></div>
	{/if}
</div>

<style>
	.service-row { display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-2) var(--space-3); background: var(--bg-tertiary); border-radius: var(--radius-sm); }
	.service-name { font-size: var(--text-subhead); font-weight: var(--fw-medium); min-width: 70px; }
	.body { display: grid; gap: var(--space-1); min-width: 0; font-size: var(--text-footnote); }
	.headline { color: var(--text-primary); }
	.detail { color: var(--text-tertiary); }
	.service-actions { margin-left: auto; display: flex; align-items: center; gap: var(--space-2); }
	summary { cursor: pointer; color: var(--accent); }
	pre { max-height: 180px; overflow: auto; margin: var(--space-2) 0 0; white-space: pre-wrap; color: var(--text-tertiary); font: var(--text-caption)/1.45 var(--font-mono, monospace); }
	@media (max-width: 768px) { .service-row { flex-wrap: wrap; gap: var(--space-2); } .service-actions { width: 100%; margin-left: 0; justify-content: flex-end; } }
</style>
