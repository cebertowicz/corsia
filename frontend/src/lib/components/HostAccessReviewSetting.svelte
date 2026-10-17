<script lang="ts">
	import { coreSettings, updateSettings } from '$lib/stores/settings';
	import { pickerModels, workspaceModels } from '$lib/stores/models';
	import { StatePill, Toggle } from '$lib/components/ui';
	import { settingsAnchor } from '$lib/stores/settingsNavigation';

	let { onError }: { onError: (message: string) => void } = $props();
	let autoApprovalSaving = $state(false);
	let fallbackSaving = $state(false);

	const readyModels = $derived($workspaceModels.filter(m => m.status === 'ready'));

	const reviewerPhantom = $derived.by(() => {
		const id = $coreSettings?.host_access_reviewer_model ?? '';
		if (!id || readyModels.some(m => m.id === id)) return null;
		return { id, name: $coreSettings?.host_access_reviewer_model_name || id };
	});

	const fallbackOn = $derived($coreSettings?.host_access_reviewer_fallback === true);
	const fixedReviewerName = $derived(
		$coreSettings?.host_access_reviewer_model
			? $coreSettings.host_access_reviewer_model_name || $coreSettings.host_access_reviewer_model
			: null,
	);

	const reviewerPill = $derived.by(() => {
		const status = $coreSettings?.host_access_reviewer_status;
		if (status === 'ready') return { state: 'success' as const, label: 'Ready' };
		if (status === 'unavailable') {
			return fallbackOn
				? { state: 'warning' as const, label: 'Using fallback' }
				: { state: 'error' as const, label: 'Unavailable' };
		}
		return { state: 'success' as const, label: "Requesting agent's model" };
	});

	async function setFallback(next: boolean) {
		onError('');
		fallbackSaving = true;
		const result = await updateSettings({ host_access_reviewer_fallback: next });
		fallbackSaving = false;
		if (!result.ok) onError(result.error ?? 'Failed to update');
	}
</script>

<div class="settings-card" data-settings-anchor="host-access-review" use:settingsAnchor={'host-access-review'}>
	<div class="card-title-row">
		<h3>Host Access Review</h3>
		<StatePill state={reviewerPill.state} label={reviewerPill.label} size="sm" />
	</div>
	<p class="card-desc">Independently reviews temporary access requests and each elevated action.</p>

	<div class="field-row">
		<label for="host-access-reviewer-model">Reviewer model</label>
		<select
			id="host-access-reviewer-model"
			value={$coreSettings?.host_access_reviewer_model ?? ''}
			onchange={async (e: Event) => {
				onError('');
				const target = e.target as HTMLSelectElement;
				const result = await updateSettings({
					host_access_reviewer_model: target.value || null,
				});
				if (!result.ok) {
					target.value = $coreSettings?.host_access_reviewer_model ?? '';
					onError(result.error ?? 'Failed to update');
				}
			}}
		>
			<option value="">Same model as the requesting agent</option>
			{#if reviewerPhantom}
				<option value={reviewerPhantom.id}>{reviewerPhantom.name} (unavailable)</option>
			{/if}
			{#each pickerModels($workspaceModels, $coreSettings?.host_access_reviewer_model) as model (model.id)}
				<option value={model.id}>{model.name}{model.desc ? ` (${model.desc})` : ''}</option>
			{/each}
		</select>
	</div>
	{#if fixedReviewerName}
		<div class="feature-row">
			<div class="feature-info">
				<span class="feature-name">Fall back to the agent's own model</span>
				<span class="feature-desc">If {fixedReviewerName} can't run, for example because of a usage limit, each requesting agent's own model reviews instead, on that agent's account. Off: requests fail until {fixedReviewerName} is back or you choose another model.</span>
			</div>
			<Toggle
				checked={fallbackOn}
				onchange={setFallback}
				ariaLabel="Fall back to the requesting agent's own model when the reviewer can't run"
				disabled={fallbackSaving}
			/>
		</div>
	{/if}
	<div class="feature-row">
		<div class="feature-info">
			<span class="feature-name">Auto-approve leases</span>
			<span class="feature-desc">When the reviewer allows an application, activate it immediately without asking you. Per-action review and hard safety limits still apply. Repository-policy exceptions still require your approval.</span>
		</div>
		<Toggle
			checked={$coreSettings?.host_access_auto_approve ?? false}
			onchange={async (next) => {
				onError('');
				autoApprovalSaving = true;
				const result = await updateSettings({ host_access_auto_approve: next });
				autoApprovalSaving = false;
				if (!result.ok) onError(result.error ?? 'Failed to update');
			}}
			ariaLabel="Automatically approve reviewed host-access leases"
			disabled={autoApprovalSaving}
		/>
	</div>
	{#if $coreSettings?.host_access_reviewer_message}
		<p class="settings-status" class:error={$coreSettings.host_access_reviewer_status === 'unavailable' && !fallbackOn}>
			{$coreSettings.host_access_reviewer_message}
		</p>
	{/if}
</div>

<style>
	.settings-card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 20px 24px; margin-bottom: 16px; }
	.settings-card h3 { font-size: var(--text-callout); font-weight: 600; color: var(--text-primary); margin: 0 0 4px; }
	.card-title-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 4px; }
	.card-title-row h3 { min-width: 0; margin: 0; }
	.card-desc { font-size: var(--text-subhead); color: var(--text-muted); margin: 0 0 16px; line-height: 1.5; }
	.settings-status { margin: -2px 0 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.45; }
	.settings-status.error { color: var(--red); }
	.field-row { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
	.field-row label { font-size: var(--text-footnote); font-weight: 500; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
	.field-row select { background: var(--bg-primary); border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); color: var(--text-primary); font-size: var(--text-subhead); font-family: inherit; padding: 8px 12px; cursor: pointer; max-width: 100%; }
	.field-row select:focus { outline: none; border-color: var(--accent); }
	.feature-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--border-subtle); }
	.feature-row:last-child { border-bottom: none; }
	.feature-info { display: flex; flex-direction: column; gap: 3px; flex: 1; }
	.feature-name { font-size: var(--text-subhead); font-weight: 500; color: var(--text-primary); }
	.feature-desc { font-size: var(--text-footnote); color: var(--text-muted); line-height: 1.5; }

	@media (max-width: 768px) {
		.feature-row { flex-direction: column; gap: 10px; }
	}
</style>
