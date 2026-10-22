<script lang="ts">
	import { coreSettings, updateSettings } from '$lib/stores/settings';
	import { pickerModels, workspaceModels } from '$lib/stores/models';
	import { Toggle } from '$lib/components/ui';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';

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

	// The reviewer's state is the model row's second line (§0.1 Status): red while requests fail,
	// amber while the fallback reviews on each agent's own account.
	const reviewerUnavailable = $derived($coreSettings?.host_access_reviewer_status === 'unavailable');
	const reviewerTone = $derived(reviewerUnavailable ? (fallbackOn ? 'attention' : 'failed') : 'quiet');
	const reviewerLine = $derived.by(() => {
		const message = $coreSettings?.host_access_reviewer_message;
		if (message) return message;
		const status = $coreSettings?.host_access_reviewer_status;
		if (status === 'ready') return 'Ready';
		if (status === 'unavailable') return fallbackOn ? 'Using fallback' : 'Unavailable';
		return '';
	});

	async function setFallback(next: boolean) {
		onError('');
		fallbackSaving = true;
		const result = await updateSettings({ host_access_reviewer_fallback: next });
		fallbackSaving = false;
		if (!result.ok) onError(result.error ?? 'Failed to update');
	}

	async function setReviewer(e: Event) {
		onError('');
		const target = e.target as HTMLSelectElement;
		const result = await updateSettings({ host_access_reviewer_model: target.value || null });
		if (!result.ok) {
			target.value = $coreSettings?.host_access_reviewer_model ?? '';
			onError(result.error ?? 'Failed to update');
		}
	}

	async function setAutoApprove(next: boolean) {
		onError('');
		autoApprovalSaving = true;
		const result = await updateSettings({ host_access_auto_approve: next });
		autoApprovalSaving = false;
		if (!result.ok) onError(result.error ?? 'Failed to update');
	}
</script>

<SettingsGroup label="Host access review" anchor="host-access-review" hint="Independently reviews temporary access requests and each elevated action.">
	<SettingsRow name="Reviewer model" sub={reviewerLine} subTone={reviewerTone} wrap stackActions>
		{#snippet actions()}
			<select id="host-access-reviewer-model" aria-label="Reviewer model" value={$coreSettings?.host_access_reviewer_model ?? ''} onchange={setReviewer}>
				<option value="">Same model as the requesting agent</option>
				{#if reviewerPhantom}
					<option value={reviewerPhantom.id}>{reviewerPhantom.name} (unavailable)</option>
				{/if}
				{#each pickerModels($workspaceModels, $coreSettings?.host_access_reviewer_model) as model (model.id)}
					<option value={model.id}>{model.name}{model.desc ? ` (${model.desc})` : ''}</option>
				{/each}
			</select>
		{/snippet}
	</SettingsRow>
	{#if fixedReviewerName}
		<SettingsRow
			name="Fall back to the agent's own model"
			sub={`If ${fixedReviewerName} can't run, for example because of a usage limit, each requesting agent's own model reviews instead, on that agent's account. Off: requests fail until ${fixedReviewerName} is back or you choose another model.`}
			wrap
		>
			{#snippet actions()}
				<Toggle
					checked={fallbackOn}
					onchange={setFallback}
					ariaLabel="Fall back to the requesting agent's own model when the reviewer can't run"
					disabled={fallbackSaving}
				/>
			{/snippet}
		</SettingsRow>
	{/if}
	<SettingsRow
		name="Auto-approve leases"
		sub="When the reviewer allows an application, activate it immediately without asking you. Per-action review and hard safety limits still apply. Repository-policy exceptions still require your approval."
		wrap
	>
		{#snippet actions()}
			<Toggle
				checked={$coreSettings?.host_access_auto_approve ?? false}
				onchange={setAutoApprove}
				ariaLabel="Automatically approve reviewed host-access leases"
				disabled={autoApprovalSaving}
			/>
		{/snippet}
	</SettingsRow>
</SettingsGroup>

<style>
	/* A select in a row's trailing slot. Field would draw a label column the row name already is,
	   so this follows Field's native-control contract (DESIGN-SYSTEM §3.4) without the Field.
	   It sizes to its longest model name up to the row's width; its row stacks it under the copy when both don't fit. */
	select {
		height: 32px; max-width: 100%;
		padding: 0 30px 0 10px;
		background-color: var(--bg-tertiary);
		background-image: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239ca3af' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
		background-repeat: no-repeat; background-position: right 10px center; background-size: 12px;
		border: 1px solid var(--border); border-radius: var(--radius-xs);
		color: var(--text-primary); font-family: inherit; font-size: var(--text-subhead);
		text-overflow: ellipsis; cursor: pointer;
		appearance: none; -webkit-appearance: none;
	}
	select:focus { outline: none; border-color: var(--accent); box-shadow: var(--shadow-focus); }
</style>
