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

{#if download}
	<ModelRow
		name={download.name ?? 'Model'}
		state={download.failed ? 'error' : 'loading'}
		meta={download.failed ? "Couldn't download this model. Try again." : `Downloading ${download.percent ?? 0}%`}
		progress={download.percent ?? 0}
		puck={false}
		onCancel={download.failed ? undefined : cancel}
		onRetry={download.failed ? retry : undefined}
		onDeleteDownload={download.failed ? deleteFailedDownload : undefined}
	/>
{/if}
{#if error}<div class="error">{error}</div>{/if}

<style>
	.error { color: var(--red); margin: var(--space-2) var(--space-3); font-size: var(--text-footnote); }
</style>
