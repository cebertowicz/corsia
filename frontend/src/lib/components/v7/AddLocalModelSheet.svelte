<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { flip } from 'svelte/animate';
	import { quintOut } from 'svelte/easing';
	import { get } from 'svelte/store';
	import { cancelLocalDownload, deleteLocalModelDownload, fetchLocalModelRecommendations, fetchSources, startLocalModelDownload, type LocalModelRecommendation, type LocalModelRecommendations, type ModelSource } from '$lib/conductorSources';
	import { token } from '$lib/stores/auth';
	import { requestSettingsSection } from '$lib/stores/settingsNavigation';
	import type { ProviderModel } from '$lib/stores/providerModels';
	import { on } from '$lib/ws/events';
	import { ms, peerFade } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import AddModelBuildRow from './AddModelBuildRow.svelte';
	import LocalModelDeleteDownloadModal from './LocalModelDeleteDownloadModal.svelte';
	import { modelGroups } from './addModelRows';

	interface Props { onClose: () => void; onStarted?: () => void; }
	const { onClose, onStarted }: Props = $props();
	let loading = $state(true);
	let error = $state('');
	let retryable = $state(true);
	let data = $state<LocalModelRecommendations | null>(null);
	let download = $state<ModelSource['settings_download'] | null>(null);
	let sort = $state<'quality' | 'speed'>('quality');
	let pending = $state<{ repository: string; action: 'start' | 'cancel' | 'delete' } | null>(null);
	const busy = $derived(Boolean(pending));
	let rowError = $state<{ repository: string; message: string } | null>(null);
	let confirmDelete = $state<{ model: ProviderModel; size: string } | null>(null);
	let stop: (() => void) | null = null;
	const groups = $derived(modelGroups(data?.models ?? [], sort, data?.recommended_repository ?? null));
	const running = $derived(Boolean(download && !download.failed && !download.finishing));
	const runningListed = $derived(groups.some((group) => group.builds.some((build) => build.repository === download?.repository)));

	async function refreshList() {
		const t = get(token);
		if (!t) { error = 'Couldn’t check which models fit this computer.'; return; }
		try {
			const result = await fetchLocalModelRecommendations(t);
			if (result.state !== 'ready') {
				error = result.message ?? 'Couldn’t check which models fit this computer.';
				retryable = !result.message;
				return;
			}
			data = result; error = '';
		} catch (caught) {
			const status = (caught as { status?: number }).status;
			error = typeof status === 'number' && caught instanceof Error ? caught.message : 'Couldn’t check which models fit this computer.';
			retryable = typeof status !== 'number' || status >= 500;
		}
	}
	async function refreshDownload() {
		const t = get(token);
		if (!t) return;
		try {
			const previous = download;
			download = (await fetchSources(t)).find((source) => source.source === 'omlx')?.settings_download ?? null;
			if (previous && !download) await refreshList();
		} catch { /* Keep the last known state if the source cannot be read. */ }
	}
	async function load() {
		loading = true;
		await Promise.all([refreshList(), refreshDownload()]);
		loading = false;
	}
	async function start(repository: string) {
		const t = get(token);
		if (!t) { rowError = { repository, message: "Couldn't start the download. Try again." }; return; }
		if (busy) return;
		pending = { repository, action: 'start' }; rowError = null;
		try { await startLocalModelDownload(t, repository); onStarted?.(); await refreshDownload(); }
		catch (caught) { rowError = { repository, message: caught instanceof Error ? caught.message : 'Couldn’t start the download. Try again.' }; }
		finally { pending = null; }
	}
	async function cancel() {
		const t = get(token);
		if (!t || busy) return;
		const repository = download?.repository ?? '';
		pending = { repository, action: 'cancel' }; rowError = null;
		try { await cancelLocalDownload(t); await refreshDownload(); }
		catch {
			const message = 'Couldn’t cancel the local model download. Try again.';
			if (runningListed) rowError = { repository, message }; else error = message;
		}
		finally { pending = null; }
	}
	async function deleteFailed(repository: string) {
		const t = get(token);
		if (!t || busy) return;
		pending = { repository, action: 'delete' }; rowError = null;
		try { await deleteLocalModelDownload(t, repository); await refreshDownload(); await refreshList(); }
		catch (caught) { rowError = { repository, message: caught instanceof Error ? caught.message : 'Couldn’t delete the download. Try again.' }; }
		finally { pending = null; }
	}
	function deleteBuild(build: LocalModelRecommendation) {
		if (!download?.downloaded) { void deleteFailed(build.repository); return; }
		confirmDelete = { model: { id: build.repository, name: build.name, state: 'default', meta: '', caps: [], sizeBytes: build.download_size_gb * 1000 ** 3 }, size: `${Math.round(build.download_size_gb)} GB` };
	}
	function sortKey(event: KeyboardEvent) {
		if (!['ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown'].includes(event.key)) return;
		event.preventDefault();
		sort = sort === 'quality' ? 'speed' : 'quality';
		const group = (event.currentTarget as HTMLElement).parentElement;
		group?.querySelector<HTMLButtonElement>(`[data-sort="${sort}"]`)?.focus();
	}
	/** A re-sorted model slides to its new place (quintOut is --ease-entrance's curve); reduced motion crossfades instead. */
	const moveParams = () => ({ duration: $prefersReducedMotion ? 0 : ms('--dur-standard'), easing: quintOut });
	function openSettings() { onClose(); requestSettingsSection('providers'); }
	onMount(() => { void load(); stop = on('models_updated', () => { void refreshDownload(); }); });
	onDestroy(() => stop?.());
</script>

<Modal title="Add model" size="md" opaque {onClose}>
	{#if loading}
		<div class="notice"><Spinner /> Checking which models fit this computer…</div>
	{:else if error}
		<div class="notice error" role="alert">{error}<Button size="sm" variant="secondary" onclick={retryable ? load : openSettings}>{retryable ? 'Retry' : 'Open settings'}</Button></div>
	{:else}
		{#if running}<div class="notice" role="status">{download?.name ?? 'A model'} is downloading. {#if !runningListed}<Button size="sm" variant="ghost" disabled={busy} loading={pending?.action === 'cancel'} onclick={cancel}>Cancel</Button>{/if}</div>{/if}
		{#if groups.length === 0}
			<div class="notice">No recommended models fit this computer.</div>
		{:else}
			<div class="summary">Picked for this computer{data?.memory_gb == null ? '' : ` · ${data.memory_gb} GB memory`}{data?.memory_in_use_gb ? `, ${data.memory_in_use_gb} GB in use` : ''}{data?.disk_free_gb == null ? '' : ` · ${data.disk_free_gb} GB disk free`}</div>
			<div class="sort">
				<div class="segmented" role="radiogroup" aria-label="Sort models">
					<button class="segment" class:active={sort === 'quality'} role="radio" data-sort="quality" onkeydown={sortKey} tabindex={sort === 'quality' ? 0 : -1} aria-checked={sort === 'quality'} onclick={() => (sort = 'quality')}>Quality</button>
					<button class="segment" class:active={sort === 'speed'} role="radio" data-sort="speed" onkeydown={sortKey} tabindex={sort === 'speed' ? 0 : -1} aria-checked={sort === 'speed'} onclick={() => (sort = 'speed')}>Speed</button>
				</div>
			</div>
			<div class="sorted">
				{#key $prefersReducedMotion ? sort : null}
					<div class="sorted-list" in:peerFade out:peerFade>
						{#each groups as group (group.name)}
							<section class="model" animate:flip={moveParams()}>
								<h3>{group.name}</h3>
								{#each group.builds as build (build.repository)}
									<AddModelBuildRow {group} {build} recommended={data?.recommended_repository === build.repository}
										running={running && download?.repository === build.repository} percent={download?.percent ?? 0}
										failed={Boolean(download?.failed && download.repository === build.repository)} downloaded={Boolean(download?.downloaded)}
										{busy} pendingAction={pending?.repository === build.repository ? pending.action : null} otherRunning={running} error={rowError?.repository === build.repository ? rowError.message : ''}
										onDownload={() => void start(build.repository)} onCancel={() => void cancel()} onDelete={() => deleteBuild(build)} />
								{/each}
							</section>
						{/each}
					</div>
				{/key}
			</div>
		{/if}
	{/if}
</Modal>
{#if confirmDelete}
	<LocalModelDeleteDownloadModal model={confirmDelete.model} size={confirmDelete.size} onClose={() => confirmDelete = null} onDeleted={async () => { await refreshDownload(); await refreshList(); }} />
{/if}

<style>
	.notice { display: flex; align-items: center; gap: var(--space-2); color: var(--text-secondary); padding: var(--space-3) 0; }
	.notice.error { color: var(--red-text); justify-content: space-between; }
	.summary { color: var(--text-secondary); font-size: var(--text-footnote); }
	.sort { margin: var(--space-3) 0; }
	/* Sort's touch targets extend beyond its visible segments on phones. */
	/* Under reduced motion the list before and after a re-sort share one cell and crossfade. */
	.sorted { display: grid; }
	.sorted-list { grid-area: 1 / 1; min-width: 0; }
	.model { padding: var(--space-3) 0; border-top: 1px solid var(--border); background: var(--bg-secondary); }
	.model h3 { margin: 0; color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-medium); }
</style>
