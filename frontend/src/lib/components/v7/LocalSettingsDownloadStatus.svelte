<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { cancelLocalDownload, deleteLocalModelDownload, fetchSources, startLocalModelDownload, type ModelSource } from '$lib/conductorSources';
	import { token } from '$lib/stores/auth';
	import { on } from '$lib/ws/events';
	import ModelRow from './ModelRow.svelte';

	interface Props { onStatusChange?: (hasDownload: boolean) => void; }
	const { onStatusChange = () => {} }: Props = $props();
	let download = $state<ModelSource['settings_download'] | null>(null);
	const sizeLabel = $derived(download?.size_bytes ? `${(download.size_bytes / 1024 ** 3).toFixed(1)} GB` : '');
	let busy = $state(false);
	let error = $state('');
	let stop: (() => void) | null = null;

	async function refresh() {
		const t = get(token);
		if (!t) return;
		try {
			download = (await fetchSources(t)).find((source) => source.source === 'omlx')?.settings_download ?? null;
			onStatusChange(download !== null);
		} catch { /* preserve the last visible download state */ }
	}

	async function retry() {
		const repository = download?.repository;
		const t = get(token);
		if (!repository || !t || busy) return;
		busy = true; error = '';
		try {
			await startLocalModelDownload(t, repository);
		} catch (caught) { error = caught instanceof Error ? caught.message : "Couldn't retry the local model download. Try again."; }
		finally { busy = false; await refresh(); }
	}

	async function cancel() {
		const t = get(token);
		if (!t || busy) return;
		busy = true; error = '';
		try { await cancelLocalDownload(t); }
		catch { error = "Couldn't cancel the local model download. Try again."; }
		finally { busy = false; await refresh(); }
	}

	async function deleteFailedDownload() {
		const repository = download?.repository;
		const t = get(token);
		if (!repository || !t || busy) return;
		busy = true; error = '';
		try { await deleteLocalModelDownload(t, repository); }
		catch (caught) { error = caught instanceof Error ? caught.message : "Couldn't delete the local model download. Try again."; }
		finally { busy = false; await refresh(); }
	}

	onMount(() => {
		void refresh();
		stop = on('models_updated', () => { void refresh(); });
	});
	onDestroy(() => { stop?.(); onStatusChange(false); });
</script>

<!-- Once the bytes are on disk the model's own row takes over; a download row there would read "Downloading 0%". -->
{#if download && !download.finishing}
	<ModelRow
		name={download.name ?? 'Model'}
		state={download.failed ? 'error' : 'default'}
		meta={download.failed ? (download.downloaded ? "Downloaded, but couldn’t be added. Retry to finish adding it." : "Couldn’t download this model. Try again.") : [sizeLabel, `Downloading ${download.percent ?? 0}%`].filter(Boolean).join(' · ')}
		progress={download.percent ?? 0}
		downloading={!download.failed}
		downloadingOn={download.will_pin === true}
		onCancel={download.failed ? undefined : cancel}
		onRetry={download.failed ? retry : undefined}
		onDeleteDownload={download.failed ? deleteFailedDownload : undefined}
	/>
{/if}
{#if error}<div class="error">{error}</div>{/if}

<style>
	.error {
		margin-left: calc(var(--space-2) + var(--project-mark-size) + var(--space-3));
		margin-right: var(--space-2);
		padding: var(--space-1) 0;
		color: var(--red-text); font-size: var(--text-caption);
	}
</style>
