<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { cancelLocalDownload, fetchSources, startLocalModelDownload, type ModelSource } from '$lib/conductorSources';
	import { token } from '$lib/stores/auth';
	import { on } from '$lib/ws/events';
	import Button from '$lib/components/ui/Button.svelte';

	let download = $state<ModelSource['settings_download'] | null>(null);
	let busy = $state(false);
	let error = $state('');
	let stop: (() => void) | null = null;

	async function refresh() {
		const t = get(token);
		if (!t) return;
		try { download = (await fetchSources(t)).find((source) => source.source === 'omlx')?.settings_download ?? null; }
		catch { /* preserve the last visible download state */ }
	}

	async function retry() {
		const repository = download?.repository;
		const t = get(token);
		if (!repository || !t || busy) return;
		busy = true; error = '';
		try {
			await startLocalModelDownload(t, repository);
		} catch { error = "Couldn't retry the local model download."; }
		finally { busy = false; await refresh(); }
	}

	async function cancel() {
		const t = get(token);
		if (!t || busy) return;
		busy = true; error = '';
		try { await cancelLocalDownload(t); }
		catch { error = "Couldn't cancel the local model download."; }
		finally { busy = false; await refresh(); }
	}

	onMount(() => {
		void refresh();
		stop = on('models_updated', () => { void refresh(); });
	});
	onDestroy(() => stop?.());
</script>

{#if download}
	<div class:failed={download.failed} class="download" aria-live="polite">
		<span>{download.failed ? `Download of ${download.name ?? 'model'} failed` : `Downloading ${download.name ?? 'model'}${download.percent == null ? '' : ` · ${download.percent}%`}`}</span>
		{#if download.failed}
			<Button size="sm" variant="secondary" disabled={busy || !download.repository} onclick={retry}>{busy ? 'Trying again…' : 'Try again'}</Button>
		{:else}
			<Button size="sm" variant="ghost" disabled={busy} onclick={cancel}>Cancel</Button>
		{/if}
	</div>
{/if}
{#if error}<div class="error">{error}</div>{/if}

<style>
	.download {
		display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
		padding: var(--space-2) var(--space-3); color: var(--text-secondary);
		border-bottom: 1px solid var(--border-subtle); font-size: var(--text-footnote);
	}
	.failed, .error { color: var(--red); }
	.error { margin: var(--space-2) var(--space-3); font-size: var(--text-footnote); }
</style>
