<script lang="ts">
	import Toggle from './ui/Toggle.svelte';
	import Message from './ui/Message.svelte';
	import { loadCloudModelsOff, saveCloudModelsOff } from '$lib/api/providerPolicy';
	import { models } from '$lib/stores/models';

	// The project's own switch: only models on this Mac or a private machine run in the project.
	let { projectId }: { projectId: string } = $props();
	let off = $state(false);
	let loaded = $state(false);
	let saving = $state(false);
	let error = $state('');
	const hasReadyLocalModel = $derived($models.some((model) => model.is_local && model.status === 'ready'));

	$effect(() => { void load(projectId); });
	async function load(id: string) {
		loaded = false; error = '';
		const result = await loadCloudModelsOff(id);
		if (result.ok) off = result.value.cloud_models_off === true;
		else error = result.error;
		loaded = true;
	}
	async function change(next: boolean) {
		if (saving) return;
		saving = true; error = '';
		const result = await saveCloudModelsOff(projectId, next);
		if (result.ok) off = result.value.cloud_models_off;
		else error = `Could not turn Cloud models off ${next ? 'on' : 'off'}: ${result.error}`;
		saving = false;
	}
</script>

<div class="setting">
	<div>
		<span class="label">Cloud models off</span>
		<p>Only models on this Mac, or on a machine you've marked private, can run in this project, including every worker its agents start.</p>
		{#if off}<p>Agents here using a cloud model stop at their next turn until they switch to a local one.</p>{/if}
	</div>
	<Toggle checked={off} disabled={saving || !loaded} ariaLabel="Cloud models off" onchange={change} />
</div>
{#if off && !hasReadyLocalModel}<Message variant="warn">No local model is ready, so nothing in this project can run until one is.</Message>{/if}
{#if error}<Message variant="err">{error}</Message>{/if}

<style>
	.setting { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
	.setting > div { min-width: 0; }
	.setting :global(.tg) { flex: 0 0 auto; }
	.label { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-medium); }
	p { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	p + p { margin-top: var(--space-1); }
</style>
