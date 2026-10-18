<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import { fetchLocalModelRecommendations, startLocalModelDownload, type LocalModelRecommendation } from '$lib/conductorSources';
	import { localModelRecommendationMeta } from '$lib/stores/localModelRecommendationMeta';
	import Modal from '$lib/components/ui/Modal.svelte';
	import ListRow from '$lib/components/ui/ListRow.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	interface Props { onClose: () => void; onStarted?: () => void; }
	const { onClose, onStarted }: Props = $props();
	let loading = $state(true);
	let error = $state('');
	let sections = $state<Record<string, LocalModelRecommendation[]>>({});
	let downloaded = $state(new Set<string>());
	let memoryGb = $state<number | null>(null);
	let diskFreeGb = $state<number | null>(null);
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
			downloaded = new Set((body.downloads ?? []).filter((entry) => entry.status === 'available').map((entry) => entry.repository));
			memoryGb = typeof body.memory_gb === 'number' ? body.memory_gb : null;
			diskFreeGb = typeof body.disk_free_gb === 'number' ? body.disk_free_gb : null;
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
	function diskNeed(model: LocalModelRecommendation): string | null {
		if (model.disk?.state !== 'insufficient') return null;
		const required = model.disk.required_bytes;
		return required ? `Needs ${Math.ceil(required / 1024 ** 3)} GB of disk` : 'Needs more disk space';
	}

	function meta(model: LocalModelRecommendation): string[] {
		return localModelRecommendationMeta(model);
	}

	function joinedMeta(model: LocalModelRecommendation): string[] {
		// Keep separators with their neighbours so narrow sheets never strand a dot at line end.
		return [meta(model).join('\u00a0·\u00a0')];
	}

	onMount(load);
</script>

<Modal title="Add model" size="sm" {onClose}>
	{#if loading}
		<div class="notice"><Spinner /> Checking which models fit this Mac…</div>
	{:else if error}
		<div class="notice error">{error}<Button size="sm" variant="secondary" onclick={load}>Try again</Button></div>
	{:else if Object.values(sections).every((models) => models.length === 0)}
		<div class="notice">No recommended models fit this Mac.</div>
	{:else}
		<div class="section-line">Picked for this Mac{memoryGb === null ? '' : ` · ${memoryGb} GB`}{diskFreeGb === null ? '' : ` · ${diskFreeGb} GB disk free`}</div>
		{#each Object.entries(sections) as [section, models]}
			{#if models.length}
				{#if section !== 'best'}<h3>{section}</h3>{/if}
				{#each models as model, index (model.repository)}
					<div class:insufficient={Boolean(diskNeed(model))}>
					<ListRow meta={joinedMeta(model)}>
						{#snippet children()}
							<span class="model-name">{model.name}</span>
							{#if section === 'best' && index === 0}<span class="recommended">Recommended</span>{/if}
							<!-- Disk space appears below the canonical meta row. -->
						{/snippet}
						{#snippet detail()}
							{#if diskNeed(model)}<span class="disk-need">{diskNeed(model)}</span>{/if}
						{/snippet}
						{#snippet actions()}
							{#if downloaded.has(model.repository)}
								<span class="downloaded">Downloaded</span>
							{:else if !diskNeed(model)}
								<Button size="sm" variant={section === 'best' && index === 0 ? 'primary' : 'chip'} loading={starting === model.repository} disabled={Boolean(starting)} onclick={() => download(model.repository)}>Download</Button>
							{/if}
						{/snippet}
					</ListRow>
					</div>
				{/each}
			{/if}
		{/each}
	{/if}
</Modal>

<style>
	.notice { display: flex; align-items: center; gap: var(--space-2); color: var(--text-secondary); padding: var(--space-3) 0; }
	.notice.error { color: var(--red); justify-content: space-between; }
	.section-line { color: var(--text-secondary); font-size: var(--text-footnote); margin: 0 0 var(--space-2); }
	h3 { color: var(--text-tertiary); font-size: var(--text-caption); font-weight: 600; margin: var(--space-3) 0 var(--space-2); }
	.model-name { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-regular); }
	.recommended { display: inline; color: var(--accent-text); font-size: var(--text-footnote); font-weight: 600; margin-left: var(--space-1); }
	.disk-need { display: block; color: var(--text-tertiary); font-size: var(--text-footnote); margin-top: var(--space-1); }
	.insufficient { opacity: 0.58; }
	.downloaded { color: var(--text-secondary); font-size: var(--text-footnote); }
</style>
