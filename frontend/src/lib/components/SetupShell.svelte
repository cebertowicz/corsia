<script lang="ts">
	import type { Snippet } from 'svelte';
	import { Button, RailRow, RailSection } from '$lib/components/ui';

	export type SetupStep = { label: string; state: 'current' | 'todo' | 'done' };
	export type SetupJob = { label: string; state: 'busy' | 'done' | 'error'; meta?: string; progress?: number };

	interface Props {
		steps: SetupStep[];
		jobs?: SetupJob[];
		title: string;
		lede?: string;
		primary?: { label: string; disabled?: boolean; loading?: boolean };
		onPrimary?: () => void;
		secondary?: Snippet;
		children?: Snippet;
	}

	const { steps, jobs = [], title, lede, primary, onPrimary, secondary, children }: Props = $props();
</script>

<div class="setup-shell">
	<aside class="rail" aria-label="Setup progress">
		<div class="traffic-space" data-tauri-drag-region></div>
		<RailSection label="Setup" first>
			{#each steps as step (step.label)}
				<RailRow label={step.label} state={step.state} />
			{/each}
		</RailSection>
		{#if jobs.length}
			<RailSection label="In the background">
				{#each jobs as job (job.label)}
					<RailRow {...job} />
				{/each}
			</RailSection>
		{/if}
	</aside>
	<main class="pane">
		<div class="drag" data-tauri-drag-region></div>
		<header><h1>{title}</h1></header>
		{#if lede}<p class="lede">{lede}</p>{/if}
		<div class="body">{@render children?.()}</div>
		<footer>
			<div>{@render secondary?.()}</div>
			{#if primary}<Button variant="pill" disabled={primary.disabled} loading={primary.loading} onclick={onPrimary}>{primary.label}</Button>{/if}
		</footer>
	</main>
</div>

<style>
	.setup-shell { min-height: 100dvh; display: flex; background: var(--bg-primary); color: var(--text-primary); }
	.rail { width: var(--sidebar-width); flex: none; border-right: 1px solid var(--border-subtle); background: var(--glass-thick); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur); }
	.traffic-space { height: calc(var(--space-7) + var(--space-5)); }
	.pane { position: relative; flex: 1; min-width: 0; min-height: 100dvh; display: flex; flex-direction: column; padding: calc(var(--space-8) + var(--space-4)) var(--space-4) var(--space-6); }
	.drag { position: absolute; inset: 0 0 auto; height: var(--space-8); }
	header { display: flex; align-items: center; min-height: var(--space-7); }
	h1 { margin: 0; font-size: var(--text-title2); font-weight: var(--fw-semibold); letter-spacing: var(--ls-title); }
	.lede { max-width: calc(var(--space-8) * 9); margin: var(--space-3) 0 0; color: var(--text-secondary); line-height: var(--lh-normal); }
	.body { flex: 1; min-height: 0; margin-top: var(--space-5); padding-bottom: var(--space-6); }
	footer { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); min-height: var(--space-7); }
</style>
