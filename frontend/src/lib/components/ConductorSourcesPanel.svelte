<script lang="ts">
	import { get } from 'svelte/store';
	import Button from '$lib/components/ui/Button.svelte';
	import { token } from '$lib/stores/auth';
	import { messages } from '$lib/stores/threads';
	import { loadModels, models } from '$lib/stores/models';
	import { on as onWebSocketEvent } from '$lib/ws/events';
	import { anyReady, fetchSources, type ModelSource } from '$lib/conductorSources';

	/**
	 * The CORSIA agent before it has a model: one line pointing at the picker. Nothing is
	 * preselected; picking in the model picker is what starts CORSIA. The install guarantees
	 * a working model, so with none ready the card renders nothing.
	 */
	let { threadId }: { threadId: string; pollMs?: number } = $props();

	let sources = $state<ModelSource[] | null>(null);
	let readFailed = $state(false);
	let retrying = $state(false);

	const ready = $derived(sources ? anyReady(sources) : false);
	const waiting = $derived($messages.some((message) => message.role === 'user'));

	async function refresh(t: string, isActive: () => boolean = () => true) {
		try {
			const rows = await fetchSources(t);
			if (!isActive()) return;
			sources = rows;
			readFailed = false;
			await loadModels();
		} catch {
			if (isActive()) readFailed = true;
		}
	}

	$effect(() => {
		const t = $token;
		void threadId;
		if (!t) return;
		let active = true;
		// Read once, untracked: a catalog refresh must not refetch the sources.
		if (get(models).length === 0) void loadModels();
		void refresh(t, () => active);
		// Every models_updated while the screen shows: a sign-in, an install, a download or a limit reset.
		const stop = onWebSocketEvent('models_updated', () => { void refresh(t, () => active); });
		return () => { active = false; stop(); };
	});

	async function tryAgain() {
		const t = get(token);
		if (!t || retrying) return;
		retrying = true;
		try { await refresh(t); } finally { retrying = false; }
	}
</script>

{#if !sources || ready}
<section class="sources-panel" aria-label="Model sources">
	<p class="headline" aria-live="polite">
		{#if readFailed && !sources}
			Couldn't read your model sources.
		{:else if !sources}
			Checking which models are ready…
		{:else}
			Pick a model from the picker below to start CORSIA.
		{/if}
	</p>
	{#if readFailed && !sources}
		<div class="actions"><Button loading={retrying} onclick={tryAgain}>Try again</Button></div>
	{/if}
	{#if waiting}
		<p class="note">Your message sends once you pick a model.</p>
	{/if}
</section>
{/if}

<style>
	.sources-panel {
		display: flex; flex-direction: column; gap: 10px;
		margin: 0 auto; width: 100%; max-width: 640px;
		padding: 14px 16px;
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		background: var(--bg-secondary);
		color: var(--text-secondary);
		font-size: var(--text-callout);
	}
	.headline { color: var(--text-primary); font-weight: 500; margin: 0; }
	.note { color: var(--text-tertiary); font-size: var(--text-footnote); margin: 0; }
	.actions { display: flex; flex-wrap: wrap; gap: 8px; }
	.actions :global(.btn) { min-height: 44px; }
</style>
