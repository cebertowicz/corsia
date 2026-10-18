<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import { fetchLocalModelRecommendations, startLocalModelDownload, type LocalModelRecommendation } from '$lib/conductorSources';
	import Modal from '$lib/components/ui/Modal.svelte';
	import ListRow from '$lib/components/ui/ListRow.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	interface Props { onClose: () => void; onStarted?: () => void; }
	const { onClose, onStarted }: Props = $props();
	let loading = $state(true);
	let error = $state('');
	let sections = $state<Record<string, LocalModelRecommendation[]>>({});
	let starting = $state('');

	async function load() {
		loading = true; error = '';
		const t = get(token);
		if (!t) {
			error = "Couldn't check which models fit this Mac.";
			loading = false;
			return;
		}
		try {
			const body = await fetchLocalModelRecommendations(t);
			if (body?.state !== 'ready') throw new Error(body?.message ?? "Couldn't check which models fit this Mac.");
			sections = body.sections && typeof body.sections === 'object' ? body.sections : {};
		} catch (caught) { error = caught instanceof Error ? caught.message : "Couldn't check which models fit this Mac."; }
		finally { loading = false; }
	}
	async function download(repository: string) {
		const t = get(token);
		if (!t) {
			error = 'Download could not start.';
			return;
		}
		starting = repository;
		try {
			await startLocalModelDownload(t, repository);
			onStarted?.(); onClose();
		} catch (caught) { error = caught instanceof Error ? caught.message : 'Download could not start.'; }
		finally { starting = ''; }
	}
	onMount(load);
</script>

<Modal title="Add local model" size="sm" {onClose}>
	{#if loading}
		<div class="notice"><Spinner /> Checking which models fit this Mac…</div>
	{:else if error}
		<div class="notice error">{error}<Button size="sm" variant="secondary" onclick={load}>Try again</Button></div>
	{:else if Object.values(sections).every((models) => models.length === 0)}
		<div class="notice">No recommended models fit this Mac.</div>
	{:else}
		{#each Object.entries(sections) as [section, models]}
			{#if models.length}
				<h3>{section === 'best' ? 'Recommended' : section}</h3>
				{#each models as model, index (model.repository)}
					<ListRow name={model.name} meta={[`${model.download_size_gb} GB download`, `${Math.round(model.context_window / 1024)}K context`, ...(model.disk?.state === 'insufficient' ? ['Not enough free disk space'] : model.disk?.state === 'unknown' ? ['Disk space unavailable'] : [])]}>
						{#snippet actions()}<Button size="sm" variant={section === 'best' && index === 0 ? 'primary' : 'secondary'} loading={starting === model.repository} disabled={Boolean(starting) || model.disk?.state !== 'ready'} onclick={() => download(model.repository)}>Download</Button>{/snippet}
					</ListRow>
				{/each}
			{/if}
		{/each}
	{/if}
</Modal>

<style>
	.notice { display: flex; align-items: center; gap: var(--space-2); color: var(--text-secondary); padding: var(--space-3) 0; }
	.notice.error { color: var(--red); justify-content: space-between; }
	h3 { color: var(--text-tertiary); font-size: var(--text-caption); letter-spacing: .08em; text-transform: uppercase; margin: var(--space-3) 0 var(--space-2); }
</style>
