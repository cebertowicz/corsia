<!--
  AttentionList.svelte — "Requires User" section (coordination MVP S6b, S1).

  Fetches raised attention items scoped to the given thread (or all if none).
  Refreshes on mount, explicit refresh(), and authenticated reconnect.
  Solid surface only (rules out glass per UX15).
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { on } from '$lib/ws/events';
	import { Section } from '$lib/components/ui';
	import AttentionCard from './AttentionCard.svelte';
	import { fetchRaised } from '$lib/api/attention';
	import type { AttentionItem } from '$lib/api/attention';

	interface Props {
		/** Scoped to this thread's subscriptions. Pass null for all. */
		threadId?: string | null;
		/** 'user' shows clearing controls; 'parent' shows dispose controls. */
		mode?: 'user' | 'parent' | 'readonly';
		/** Demo items — bypass network reads and reconnect refresh (for /_design). */
		demo?: AttentionItem[] | null;
	}

	const { threadId = null, mode = 'user', demo = null }: Props = $props();

	let items = $state<AttentionItem[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let generation = 0;

	async function load() {
		const requestGeneration = ++generation;
		if (demo !== null) {
			items = demo;
			loading = false;
			error = null;
			return;
		}
		loading = true;
		error = null;
		try {
			const tok = get(token);
			if (!tok) return;
			const res = await fetchRaised(tok, threadId);
			if (requestGeneration !== generation) return;
			// Sort: high urgency first, then raised before deferred
			items = [...res.items].sort((a, b) => {
				const urgencyOrder = { high: 0, medium: 1, low: 2 } as Record<string, number>;
				const au = urgencyOrder[a.urgency ?? 'low'] ?? 2;
				const bu = urgencyOrder[b.urgency ?? 'low'] ?? 2;
				if (au !== bu) return au - bu;
				// raised before deferred
				if (a.attention_state === 'raised' && b.attention_state !== 'raised') return -1;
				if (b.attention_state === 'raised' && a.attention_state !== 'raised') return 1;
				return 0;
			});
		} catch (e: unknown) {
			if (requestGeneration === generation) {
				error = e instanceof Error ? e.message : 'Could not load items';
			}
		} finally {
			if (requestGeneration === generation) loading = false;
		}
	}

	/** Called by parent (e.g. after a WS event) to pull fresh data. */
	export function refresh() {
		load();
	}

	function handleDone(artifactId: string, _outcome: string) {
		// Optimistic: remove from list immediately, then refresh for truth
		items = items.filter((it) => it.external_id !== artifactId);
		load();
	}

	$effect(() => {
		// Fires on mount AND when threadId changes — the single initial-fetch path.
		void threadId;
		load();
	});

	onMount(() => {
		if (demo !== null) return;
		return on('connected', () => load());
	});
</script>

{#if loading && !items.length}
	<div class="al-loading" aria-busy="true">
		<span class="dot"></span><span class="dot"></span><span class="dot"></span>
	</div>
{:else if error}
	<p class="al-error">{error}</p>
{:else if items.length === 0}
	<!-- Empty state: hidden when no items — the section is invisible, not an empty card -->
{:else}
	<Section title="Requires User">
		<div class="al-list">
			{#each items as item (item.external_id)}
				<AttentionCard {item} {mode} onDone={handleDone} />
			{/each}
		</div>
	</Section>
{/if}

<style>
	.al-loading {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 10px 14px;
	}
	.dot {
		display: inline-block;
		width: 5px;
		height: 5px;
		border-radius: var(--radius-circle);
		background: var(--text-tertiary);
		animation: pulse 1.4s ease-in-out infinite;
	}
	.dot:nth-child(2) { animation-delay: 0.2s; }
	.dot:nth-child(3) { animation-delay: 0.4s; }

	.al-error {
		font-size: var(--text-footnote);
		color: var(--red);
		padding: 8px 14px;
		margin: 0;
	}

	.al-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
</style>
