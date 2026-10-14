<script lang="ts">
	import { Button } from '$lib/components/ui';
	import StatePill from '$lib/components/ui/StatePill.svelte';
	import { openUpdatePanel, openUpdateSheet, updateInFlight, updateOperation, updateStatus } from '$lib/update/store';
	import { failureCopy, failureFromOperation, formatInstalledAt, isFailedOperation, stepMessage } from '$lib/update/model';

	const operation = $derived($updateOperation);
	const running = $derived($updateInFlight);
	const refusal = $derived(isFailedOperation(operation) ? failureCopy(failureFromOperation(operation)) : null);
	const completedVersion = $derived(operation?.status === 'complete' ? operation.target_version : null);
	const currentVersion = $derived($updateStatus?.current_version ?? 'Unknown');
	const targetVersion = $derived(operation?.target_version ?? $updateStatus?.target_version);
	const showAvailable = $derived(!running && !refusal && $updateStatus?.available && targetVersion);
	const installedAt = $derived(operation?.settled_at ? formatInstalledAt(operation.settled_at, Date.now()) : null);
</script>

<div class="service-row update-row">
	<span class="service-name">Version</span>
	<div class="version-state">
		<span class="version-line"><span class="service-version">{currentVersion}</span>{#if refusal}<StatePill state="warning" size="sm" label="Update didn't start" />{:else if showAvailable}<StatePill state="active" size="sm" label={`${targetVersion} available`} />{/if}</span>
		{#if running}
			<span class="updating"><span class="spinner" aria-hidden="true"></span>Updating to {targetVersion ?? 'the latest version'}</span>
			<span class="step">{stepMessage(operation, false)}</span>
		{/if}
		{#if installedAt}<span class="installed">Installed {installedAt}</span>{/if}
	</div>
	<div class="service-actions">
		{#if running}
			<Button size="sm" onclick={openUpdatePanel}>Show progress</Button>
		{:else if refusal?.retryLabel}
			<Button size="sm" onclick={openUpdateSheet}>{refusal.retryLabel}</Button>
		{:else if $updateStatus?.available}
			<Button variant="primary" size="sm" onclick={openUpdateSheet}>Update</Button>
		{/if}
	</div>
</div>

{#if completedVersion || refusal}
	<div class="service-row update-row last-update">
		<span class="service-name">Last update</span>
		<div class="version-state">
			{#if completedVersion}
				<span class="service-status">Updated to {completedVersion}</span>
			{:else if refusal}
				<span class="service-status">{refusal.body || refusal.headline}</span>
				{#if operation?.conductor_told_at}<span class="conductor">⌁ Your workspace conductor has been told.</span>{/if}
			{/if}
		</div>
		{#if refusal}<div class="service-actions"><Button size="sm" onclick={openUpdateSheet}>Details</Button></div>{/if}
	</div>
{/if}

<style>
	.service-row { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-2) var(--space-3); background: var(--bg-tertiary); border-radius: var(--radius-sm); }
	.service-name { font-size: var(--text-subhead); font-weight: var(--fw-medium); min-width: 70px; }
	.version-state { display: grid; gap: var(--space-1); min-width: 0; }
	.version-line { display: inline-flex; align-items: center; gap: var(--space-2); min-width: 0; }
	.installed { color: var(--text-tertiary); font-size: var(--text-caption); }
	.service-version { color: var(--text-primary); font-size: var(--text-footnote); }
	.service-status, .step, .conductor { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.updating { color: var(--text-primary); font-size: var(--text-footnote); display: inline-flex; align-items: center; gap: var(--space-1); }
	.spinner { width: 12px; height: 12px; border: 2px solid color-mix(in srgb, var(--accent) 25%, transparent); border-top-color: var(--accent); border-radius: var(--radius-circle); animation: spin 0.8s linear infinite; }
	.service-actions { margin-left: auto; display: flex; align-items: center; gap: var(--space-2); }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (max-width: 768px) { .service-row { flex-wrap: wrap; gap: var(--space-2); } .service-actions { width: 100%; margin-left: 0; } }
</style>
