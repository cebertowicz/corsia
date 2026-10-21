<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { get } from 'svelte/store';
	import AddLocalModelSheet from './AddLocalModelSheet.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import { fetchSources, type ModelSource } from '$lib/conductorSources';
	import { token } from '$lib/stores/auth';
	import { on } from '$lib/ws/events';
	import { LOW_MEMORY_LOCAL_MODEL_MESSAGE } from '$lib/setupLocalPick.svelte';

	interface Props {
		onStarted: () => void;
		onAddProvider: () => void;
		empty?: boolean;
	}

	const { onStarted, onAddProvider, empty = false }: Props = $props();
	let source = $state<ModelSource | undefined>(undefined);
	let showAddLocal = $state(false);
	let stop: (() => void) | null = null;
	const lowMemory = $derived(source?.eligible === false);

	async function refresh() {
		const t = get(token);
		if (!t) return;
		try { source = (await fetchSources(t)).find((candidate) => candidate.source === 'omlx'); }
		catch { /* Keep the last known eligibility; controls must not invent a fallback. */ }
	}

	onMount(() => {
		void refresh();
		stop = on('models_updated', () => { void refresh(); });
	});
	onDestroy(() => stop?.());
</script>

{#if lowMemory}
	<div class="local-refusal">
		<p>{LOW_MEMORY_LOCAL_MODEL_MESSAGE}</p>
		<Button size="sm" variant="secondary" onclick={onAddProvider}>Add provider</Button>
	</div>
{:else}
	<div class="add-local">
		<Button size="sm" variant={empty ? 'primary' : 'ghost'} onclick={() => showAddLocal = true}>Add model</Button>
	</div>
	{#if showAddLocal}
		<AddLocalModelSheet onClose={() => showAddLocal = false} {onStarted} />
	{/if}
{/if}

<style>
	.add-local, .local-refusal { padding: var(--space-2) var(--space-3); }
	.local-refusal { color: var(--text-secondary); font-size: var(--text-footnote); }
	.local-refusal p { margin: 0 0 var(--space-1); }
</style>
