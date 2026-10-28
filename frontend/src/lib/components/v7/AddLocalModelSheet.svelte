<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import { fetchLocalModelRecommendations, fetchSources, startLocalModelDownload, type LocalModelRecommendation } from '$lib/conductorSources';
	import { localModelRecommendationMeta } from '$lib/stores/localModelRecommendationMeta';
	import Modal from '$lib/components/ui/Modal.svelte';
	import ListRow from '$lib/components/ui/ListRow.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	interface Props { onClose: () => void; onStarted?: () => void; }
	const { onClose, onStarted }: Props = $props();
	let loading = $state(true);
	let error = $state('');
	/** Whether asking again can change the answer: not for a cause the person must fix first, such as Set up or an oMLX update. */
	let retryable = $state(true);
	let models = $state<LocalModelRecommendation[]>([]);
	let downloaded = $state(new Set<string>());
	let memoryGb = $state<number | null>(null);
	let memoryRoomGb = $state<number | null>(null);
	/** Setup's pick on this list, from the backend's one fit rule; no pick, no Recommended row. */
	let recommendedRepository = $state<string | null>(null);
	let diskFreeGb = $state<number | null>(null);
	let starting = $state('');
	/** The model already downloading (Settings or setup), named before the person picks another. */
	let runningName = $state<string | null>(null);
	let runningRepository = $state<string | null>(null);

	async function load() {
		loading = true; error = ''; retryable = true;
		const t = get(token);
		if (!t) {
			error = "Couldn’t check which models fit this computer.";
			loading = false;
			return;
		}
		try {
			const [body, sources] = await Promise.all([
				fetchLocalModelRecommendations(t),
				fetchSources(t).catch(() => []),
			]);
			const running = sources.find((source) => source.source === 'omlx')?.settings_download;
			runningName = running && !running.failed ? running.name ?? 'A model' : null;
			runningRepository = running && !running.failed ? running.repository : null;
			if (body?.state !== 'ready') {
				error = body?.message ?? "Couldn’t check which models fit this computer.";
				retryable = body?.message === undefined;
				return;
			}
			models = Array.isArray(body.models) ? body.models : [];
			downloaded = new Set((body.downloads ?? []).filter((entry) => entry.status === 'available').map((entry) => entry.repository));
			memoryGb = typeof body.memory_gb === 'number' ? body.memory_gb : null;
			memoryRoomGb = typeof body.memory_room_gb === 'number' ? body.memory_room_gb : null;
			recommendedRepository = typeof body.recommended_repository === 'string' ? body.recommended_repository : null;
			diskFreeGb = typeof body.disk_free_gb === 'number' ? body.disk_free_gb : null;
		} catch (caught) {
			// A backend answer carries its status and its own sentence; anything else is a failed request.
			const status = (caught as { status?: number }).status;
			error = typeof status === 'number' && caught instanceof Error ? caught.message : "Couldn’t check which models fit this computer.";
			retryable = typeof status !== 'number' || status >= 500;
		}
		finally { loading = false; }
	}
	async function download(repository: string) {
		const t = get(token);
		if (!t) {
			error = "Couldn't start the download. Try again.";
			return;
		}
		starting = repository;
		try {
			await startLocalModelDownload(t, repository);
			onStarted?.(); onClose();
		} catch (caught) { error = caught instanceof Error ? caught.message : "Couldn't start the download. Try again."; }
		finally { starting = ''; }
	}
	function diskNeed(model: LocalModelRecommendation): string | null {
		if (model.disk?.state !== 'insufficient') return null;
		const required = model.disk.required_bytes;
		return required ? `Needs ${Math.ceil(required / 1000 ** 3)} GB of disk` : 'Needs more disk space';
	}

	// A row the backend says doesn't fit now names its need, sized from what oMLX says the model needs
	// loaded, never from its download; no figure, no line. The backend's verdict is the only fit rule.
	function memoryNeed(model: LocalModelRecommendation): string | null {
		const resident = model.resident_bytes;
		if (model.fits_now !== false || memoryRoomGb === null || typeof resident !== 'number' || resident <= 0) return null;
		return `Needs ${Math.ceil(resident / 1024 ** 3)} GB of memory · ${Math.floor(memoryRoomGb)} GB free for a model now`;
	}

	// The backend orders the list: models that fit the memory free now first, then the rest.
	const fitting = $derived(models.filter((model) => model.fits_now !== false));
	const needMore = $derived(models.filter((model) => model.fits_now === false));
	// Quality in half steps: 3.5 reads as three and a half dots, never rounded up to four.
	const level = (model: LocalModelRecommendation) => Math.min(5, Math.max(0, Math.round((model.smarts_level ?? 0) * 2) / 2));
	const dotFill = (model: LocalModelRecommendation, n: number) => (level(model) >= n ? 'on' : level(model) >= n - 0.5 ? 'half' : 'off');

	function meta(model: LocalModelRecommendation): string[] {
		return localModelRecommendationMeta(model);
	}

	function joinedMeta(model: LocalModelRecommendation): string[] {
		// Keep separators with their neighbours so narrow sheets never strand a dot at line end.
		return [meta(model).join('\u00a0·\u00a0')];
	}

	onMount(load);
</script>

{#snippet modelRow(model: LocalModelRecommendation)}
	<div class="model-row" class:insufficient={Boolean(diskNeed(model))}>
		<ListRow meta={joinedMeta(model)}>
			{#snippet children()}
				<span class="model-name">{model.name}</span>
				{#if model.smarts_level !== undefined}
					<span class="quality" role="img" aria-label={`Quality ${level(model)} of 5`}>
						{#each [1, 2, 3, 4, 5] as n (n)}<span class="dot {dotFill(model, n)}"></span>{/each}
					</span>
				{/if}
				{#if model.repository === recommendedRepository}<span class="recommended">Recommended</span>{/if}
			{/snippet}
			{#snippet detail()}
				{#if diskNeed(model)}<span class="disk-need">{diskNeed(model)}</span>{/if}
				{#if memoryNeed(model)}<span class="disk-need">{memoryNeed(model)}</span>{/if}
			{/snippet}
			{#snippet actions()}
				{#if downloaded.has(model.repository)}
					<span class="downloaded">Downloaded</span>
				{:else if runningRepository === model.repository}
					<span class="downloaded">Downloading</span>
				{:else if !diskNeed(model)}
					<Button size="sm" variant={model.repository === recommendedRepository ? 'primary' : 'chip'} loading={starting === model.repository} disabled={Boolean(starting) || runningName !== null} onclick={() => download(model.repository)}>Download</Button>
				{/if}
			{/snippet}
		</ListRow>
	</div>
{/snippet}

<Modal title="Add model" size="sm" {onClose}>
	{#if loading}
		<div class="notice"><Spinner /> Checking which models fit this computer…</div>
	{:else if error}
		<div class="notice error">{error}{#if retryable}<Button size="sm" variant="secondary" onclick={load}>Retry</Button>{/if}</div>
	{:else if models.length === 0}
		<div class="notice">No recommended models fit this computer.</div>
	{:else}
		{#if runningName}<div class="notice running" role="status">{runningName} is downloading. Add another when it finishes, or cancel it on the oMLX row.</div>{/if}
		<div class="section-line">Picked for this computer{memoryGb === null ? '' : ` · ${memoryGb} GB`}{diskFreeGb === null ? '' : ` · ${diskFreeGb} GB disk free`}</div>
		{#if fitting.length === 0 && memoryRoomGb !== null}<div class="notice">Nothing fits the {Math.floor(memoryRoomGb)} GB free for a model now.</div>{/if}
		{#each fitting as model (model.repository)}
			{@render modelRow(model)}
		{/each}
		{#if needMore.length}
			<h3>Needs more free memory</h3>
			{#each needMore as model (model.repository)}
				{@render modelRow(model)}
			{/each}
		{/if}
	{/if}
</Modal>

<style>
	.notice { display: flex; align-items: center; gap: var(--space-2); color: var(--text-secondary); padding: var(--space-3) 0; }
	.notice.error { color: var(--red); justify-content: space-between; }
	.notice.running { padding-top: 0; font-size: var(--text-footnote); }
	.section-line { color: var(--text-secondary); font-size: var(--text-footnote); margin: 0 0 var(--space-2); }
	h3 { color: var(--text-tertiary); font-size: var(--text-caption); font-weight: 600; margin: var(--space-3) 0 var(--space-2); }
	.model-name { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-regular); }
	.recommended { display: inline; color: var(--accent-text); font-size: var(--text-footnote); font-weight: 600; margin-left: var(--space-1); }
	.disk-need { display: block; color: var(--text-tertiary); font-size: var(--text-footnote); margin-top: var(--space-1); }
	/* One gap between rows whether or not a row carries a need line. */
	.model-row + .model-row { margin-top: var(--space-3); }
	.model-row :global(.row-detail:empty) { display: none; }
	.insufficient { opacity: 0.58; }
	.quality { display: inline-flex; gap: var(--space-1); margin-left: var(--space-2); vertical-align: middle; }
	.dot { box-sizing: border-box; width: 0.5em; height: 0.5em; border-radius: var(--radius-circle); background: var(--bg-tertiary); border: 1px solid var(--border); }
	.dot.on { background: var(--text-secondary); border-color: var(--text-secondary); }
	.dot.half { background: linear-gradient(to right, var(--text-secondary) 50%, var(--bg-tertiary) 50%); border-color: var(--text-secondary); }
	.downloaded { color: var(--text-secondary); font-size: var(--text-footnote); }
</style>
