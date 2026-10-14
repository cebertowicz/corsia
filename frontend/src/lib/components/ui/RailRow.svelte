<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * One row of the shell's sidebar: icon, label, and an optional second line, with the accent left bar and
	 * tint when it is the current one. Approved for setup; it mirrors the existing sidebar rail without changing
	 * shared sidebar behavior. Any new shared use needs a Product Experience decision.
	 *
	 * `state` is what the row stands for: `current` is the selected row, `idle` is a row you can pick (a list
	 * of choices), `todo` is not reached yet, `done`
	 * is finished (a check replaces the icon, and the row is a button when it has `onclick`), `busy` is work
	 * running behind it (a spinner), `error` is work that failed (a red dot and a short reason in `meta`).
	 * `progress` draws the hairline under the text; it is the only progress bar in setup.
	 */
	interface Props {
		label: string;
		state?: 'current' | 'idle' | 'todo' | 'done' | 'busy' | 'error';
		/** The second line, in caption type. */
		meta?: string;
		/** 0 to 1: a hairline under the text. */
		progress?: number;
		/** The row's own icon, for `current` and `todo`. */
		icon?: Snippet;
		/** The approved setup-step icon when a custom snippet is unnecessary. */
		iconName?: 'check' | 'person' | 'chip';
		onclick?: () => void;
	}

	const { label, state = 'todo', meta, progress, icon, iconName, onclick }: Props = $props();
	const interactive = $derived(!!onclick && state !== 'todo' && state !== 'current');
</script>

{#snippet inner()}
	<span class="lead" aria-hidden="true">
		{#if state === 'done'}
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
		{:else if state === 'busy'}
			<svg class="spinner" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M21 12a9 9 0 11-6.219-8.56" /></svg>
		{:else if state === 'error'}
			<i class="reddot"></i>
		{:else if icon}
			{@render icon()}
		{:else if iconName === 'check'}
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
		{:else if iconName === 'person'}
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="8" r="3" /><path d="M5.5 20c.7-3.5 3-5.5 6.5-5.5s5.8 2 6.5 5.5" /></svg>
		{:else if iconName === 'chip'}
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M18 9h4M2 15h4M18 15h4" /></svg>
		{/if}
	</span>
	<span class="copy">
		<span class="lbl">{label}</span>
		{#if meta}<span class="meta">{meta}</span>{/if}
		{#if progress !== undefined}
			<span class="hair" role="progressbar" aria-label={label} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(progress * 100)}><i style:width={`${Math.round(progress * 100)}%`}></i></span>
		{/if}
	</span>
{/snippet}

{#if interactive}
	<button type="button" class="row {state}" {onclick}>{@render inner()}</button>
{:else}
	<div class="row {state}" aria-current={state === 'current' ? 'step' : undefined}>{@render inner()}</div>
{/if}

<style>
	.row {
		position: relative; display: flex; align-items: flex-start; gap: 10px; width: 100%; text-align: left;
		padding: 7px 10px; border-radius: var(--radius-sm); background: none; border: none;
		font-family: inherit; font-size: var(--text-subhead); color: var(--text-secondary);
		transition: background var(--dur-tight) ease, color var(--dur-tight) ease;
	}
	button.row { cursor: pointer; }
	button.row:hover { background: var(--bg-hover); color: var(--text-primary); }
	.row.todo { color: var(--text-tertiary); }
	.row.current { background: var(--accent-wash); color: var(--text-primary); }
	.row.current .lbl { font-weight: var(--fw-medium); }
	.row.current::before {
		content: ''; position: absolute; left: 0; top: 5px; bottom: 5px; width: 2px;
		background: var(--accent-line); border-radius: 0 2px 2px 0;
	}
	.lead { flex: none; width: 16px; height: 1.5em; display: flex; align-items: center; justify-content: center; }
	.lead :global(svg) { flex: none; opacity: 0.85; }
	.row.current .lead { color: var(--accent-line); }
	.row.current .lead :global(svg) { opacity: 1; }
	.row.done .lead, .row.busy .lead { color: var(--accent-line); }
	.reddot { width: calc(var(--space-1) * 2); height: calc(var(--space-1) * 2); border-radius: var(--radius-circle); background: var(--red); }
	.spinner { animation: spin var(--dur-hero-plus) linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }

	.copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
	.lbl { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.meta { color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.2; }
	.row.error .meta { color: var(--red-text); }
	.hair { display: block; height: calc(var(--space-1) / 2); margin-top: var(--space-1); border-radius: var(--radius-pill); background: var(--border); overflow: hidden; }
	.hair i { display: block; height: 100%; background: var(--accent-line); border-radius: inherit; transition: width var(--dur-standard) var(--ease-material); }
</style>
