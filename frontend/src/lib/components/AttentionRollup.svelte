<!--
  AttentionRollup.svelte — pull-only parent roll-up (coordination MVP S6b, S6).

  Visible to a parent/conductor thread to see active requires-user items across
  its children. NEVER emits badge pressure or wake chaining — the parent polls
  on demand (GET, no push). Hidden when no active children.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { fetchRollup } from '$lib/api/attention';
	import type { AttentionItem } from '$lib/api/attention';

	interface Props {
		parentThreadId: string;
		/** Demo items — bypasses fetch (for /_design). */
		demo?: AttentionItem[] | null;
	}

	const { parentThreadId, demo = null }: Props = $props();

	let items = $state<AttentionItem[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let expanded = $state(false);

	async function load() {
		if (demo !== null) {
			items = demo ?? [];
			return;
		}
		loading = true;
		error = null;
		try {
			const tok = get(token);
			if (!tok) return;
			const res = await fetchRollup(tok, parentThreadId);
			items = res.items;
		} catch (e: unknown) {
			error = e instanceof Error ? e.message : 'Failed';
		} finally {
			loading = false;
		}
	}

	export function refresh() { load(); }

	onMount(() => { load(); });

	const visible = $derived(items.filter((it) => it.attention_state === 'raised'));
	const raised = $derived(visible.length);
</script>

{#if raised > 0}
	<div class="rollup" aria-label="Child threads requiring attention">
		<button
			class="rollup-header press-tint"
			onclick={() => (expanded = !expanded)}
			aria-expanded={expanded}
		>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v.01M12 12v4"/></svg>
			<span class="rollup-label">
				{raised} item{raised === 1 ? '' : 's'} pending in child threads
			</span>
			<svg
				class="chevron"
				class:open={expanded}
				width="12"
				height="12"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
			>
				<path d="M6 9l6 6 6-6"/>
			</svg>
		</button>

		{#if expanded}
			<ul class="rollup-list" role="list">
				{#each visible as item (item.external_id)}
					<li class="rollup-row">
						<span class="row-indicator" class:high={item.urgency === 'high'} class:medium={item.urgency === 'medium'}></span>
						<span class="row-body">
							<span class="row-action">{item.requested_action ?? item.type ?? 'Requires attention'}</span>
							{#if item.owner_thread}
								<span class="row-thread">{item.owner_thread}</span>
							{/if}
						</span>
					</li>
				{/each}
			</ul>
			<p class="rollup-note">Pull-only — no notifications sent. Refresh to update.</p>
		{/if}
	</div>
{/if}

{#if error}
	<p class="rollup-error">{error}</p>
{/if}

<style>
	.rollup {
		background: var(--bg-secondary);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		overflow: hidden;
	}

	.rollup-header {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 9px 12px;
		background: none;
		border: none;
		cursor: pointer;
		text-align: left;
		color: var(--text-secondary);
		font-size: var(--text-subhead);
		transition: background var(--dur-tight);
	}
	.rollup-header:hover { background: var(--bg-hover); }
	.rollup-header svg:first-child { flex-shrink: 0; color: var(--amber); }

	.rollup-label { flex: 1; }

	.chevron {
		flex-shrink: 0;
		transition: transform 0.15s ease;
	}
	.chevron.open { transform: rotate(180deg); }

	.rollup-list {
		list-style: none;
		margin: 0;
		padding: 0 12px 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.rollup-row {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 5px 0;
		border-top: 1px solid var(--border-subtle);
	}

	.row-indicator {
		flex-shrink: 0;
		margin-top: 5px;
		width: 6px;
		height: 6px;
		border-radius: var(--radius-circle);
		background: var(--text-tertiary);
	}
	.row-indicator.high { background: var(--red); }
	.row-indicator.medium { background: var(--amber); }

	.row-body {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.row-action {
		font-size: var(--text-subhead);
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-thread {
		font-size: var(--text-footnote);
		color: var(--text-tertiary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.rollup-note {
		font-size: var(--text-footnote);
		color: var(--text-tertiary);
		font-style: italic;
		padding: 4px 12px 8px;
		margin: 0;
	}

	.rollup-error {
		font-size: var(--text-footnote);
		color: var(--red);
		padding: 4px 0;
		margin: 0;
	}
</style>
