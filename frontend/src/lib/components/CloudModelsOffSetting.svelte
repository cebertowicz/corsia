<script lang="ts">
	import Toggle from './ui/Toggle.svelte';
	import { loadCloudModelsOff, saveCloudModelsOff } from '$lib/api/providerPolicy';
	import { models } from '$lib/stores/models';
	import { neverLocalHost, watchLocalStatus } from '$lib/stores/localPickerStatus';
	import { requestSettingsSection } from '$lib/stores/settingsNavigation';
	import { saveFailureText } from '$lib/saveFailure';

	// The project's own switch: only models on this computer or a trusted device run in the project.
	let { projectId, onCloudModelsOffState, onCloudModelsOffSaved }: { projectId: string; onCloudModelsOffState?: (value: boolean) => void; onCloudModelsOffSaved?: () => void } = $props();
	let off = $state(false);
	let loaded = $state(false);
	let saving = $state(false);
	let error = $state('');
	let loadFailed = $state(false);
	const hasReadyLocalModel = $derived($models.some((model) => model.is_local && model.status === 'ready'));

	$effect(() => { void load(projectId); });
	// The Set up link is offered only where a local model can run: the server says when it never can.
	$effect(() => watchLocalStatus());
	async function load(id: string) {
		loaded = false; error = ''; loadFailed = false;
		const result = await loadCloudModelsOff(id);
		if (result.ok) {
			off = result.value.cloud_models_off === true;
			onCloudModelsOffState?.(off);
		} else {
			loadFailed = true;
			error = result.reached === false ? 'Couldn’t load this setting. Check your connection.' : 'Couldn’t load this setting.';
			// The setting is unknown, so its switch stays hidden; account controls show as they would without it.
			onCloudModelsOffState?.(false);
		}
		loaded = true;
	}
	async function change(next: boolean) {
		if (saving) return;
		saving = true; error = '';
		const result = await saveCloudModelsOff(projectId, next);
		if (result.ok) {
			off = result.value.cloud_models_off;
			onCloudModelsOffState?.(off);
			onCloudModelsOffSaved?.();
		} else error = saveFailureText(result.detail, result.reached !== false);
		saving = false;
	}
</script>

<div class="setting">
	<div>
		<span class="label">Local models only</span>
		<p>Chats and agents in this project use only models on this computer or on a trusted device. A chat on a cloud model waits until you switch it to a local one.</p>
		{#if !hasReadyLocalModel}
			<p class="status" class:warning={off}>{off ? ($neverLocalHost ? 'No local model is ready. Connect a model on a trusted device, or turn off Local models only.' : 'No local model is ready, so nothing in this project can run.') : 'No local model is ready.'} <button type="button" onclick={() => requestSettingsSection('providers')}>{$neverLocalHost ? 'Connect a provider' : 'Set up a local model'}</button></p>
		{/if}
		{#if error}<p class="status error" role="alert">{error} {#if loadFailed}<button type="button" onclick={() => load(projectId)}>Try again</button>{/if}</p>{/if}
	</div>
	{#if !loadFailed}<Toggle checked={off} disabled={saving || !loaded} ariaLabel="Local models only" onchange={change} />{/if}
</div>

<style>
	.setting { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
	.setting > div { min-width: 0; }
	.setting :global(.tg) { flex: 0 0 auto; }
	.label { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-medium); }
	p { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	p + p { margin-top: var(--space-1); }
	.status { color: var(--text-secondary); }
	.status.warning { color: var(--amber-text); }
	.status.error { color: var(--red-text); }
	.status button { appearance: none; border: 0; padding: 0; background: transparent; color: var(--accent-text); cursor: pointer; font: inherit; font-size: inherit; text-decoration: underline; }
</style>
