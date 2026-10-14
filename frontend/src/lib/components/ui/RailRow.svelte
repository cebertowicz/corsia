<script lang="ts">
	import type { Snippet } from 'svelte';
	import { fade, fly } from 'svelte/transition';

	/**
	 * One row of the setup rail: icon, label, and an optional second line, with the accent left bar and tint when it
	 * is current. Any new shared use needs a Product Experience decision.
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
		iconName?: 'sparkle' | 'person' | 'chip';
		onclick?: () => void;
	}

	const { label, state = 'todo', meta, progress, icon, iconName, onclick }: Props = $props();
	const interactive = $derived(!!onclick && state !== 'todo' && state !== 'current');
	const hasDetails = $derived(Boolean(meta || progress !== undefined));

	function tokenFade(node: Element) {
		return fade(node, { duration: Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-tight')) || 0 });
	}

	function leadMotion(node: Element, _params: undefined, { direction }: { direction: 'in' | 'out' | 'both' }) {
		const tight = Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-tight')) || 0;
		return fade(node, { duration: direction === 'out' ? tight * 0.5 : tight });
	}

	function railMotion(node: Element) {
		const duration = Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-standard')) || 0;
		return window.matchMedia('(prefers-reduced-motion: reduce)').matches
			? fade(node, { duration: Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-tight')) || 0 })
			: fly(node, { y: 4, duration });
	}
</script>

{#snippet inner()}
	<span class="lead" aria-hidden="true">
		{#key state}
			<span class="lead-state" in:leadMotion out:leadMotion>
				{#if state === 'done'}
					<svg class="done-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5" /></svg>
				{:else if state === 'busy'}
					<svg class="spinner" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M21 12a9 9 0 11-6.219-8.56" /></svg>
				{:else if state === 'error'}
					<i class="reddot"></i>
				{:else if icon}
					{@render icon()}
				{:else if iconName === 'sparkle'}
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8L12 2Z" /><path d="m19 15 .7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7L19 15Z" /></svg>
				{:else if iconName === 'person'}
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="8" r="3" /><path d="M5.5 20c.7-3.5 3-5.5 6.5-5.5s5.8 2 6.5 5.5" /></svg>
				{:else if iconName === 'chip'}
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M18 9h4M2 15h4M18 15h4" /></svg>
				{/if}
			</span>
		{/key}
	</span>
	<span class="copy">
		<span class="lbl">{label}</span>
		<span class="details" class:open={hasDetails}>
			<span class="details-inner">
				{#if meta}<span class="meta" in:tokenFade out:tokenFade>{meta}</span>{/if}
				{#if progress !== undefined}
					<span class="hair" in:tokenFade out:tokenFade role="progressbar" aria-label={label} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(progress * 100)}><i style:width={`${Math.round(progress * 100)}%`}></i></span>
				{/if}
			</span>
		</span>
	</span>
{/snippet}

{#if interactive}
	<button type="button" class="row {state}" in:railMotion out:railMotion {onclick}>{@render inner()}</button>
{:else}
	<div class="row {state}" in:railMotion out:railMotion aria-current={state === 'current' ? 'step' : undefined}>{@render inner()}</div>
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
	button.row:active { background: var(--accent-wash); }
	@media (prefers-reduced-motion: reduce) { button.row { transition: background var(--dur-tight) var(--ease-snap), color var(--dur-tight) var(--ease-snap); } }
	.row.todo { color: var(--text-tertiary); }
	.row.current { background: var(--accent-wash); color: var(--text-primary); }
	.row.current .lbl { font-weight: var(--fw-medium); }
	.row.current::before {
		content: ''; position: absolute; left: 0; top: 5px; bottom: 5px; width: 2px;
		background: var(--accent-line); border-radius: 0 2px 2px 0;
	}
	.lead { flex: none; width: 16px; height: 1.5em; display: grid; place-items: center; }
	.lead-state { grid-area: 1 / 1; display: grid; place-items: center; }
	.lead :global(svg) { flex: none; opacity: 0.85; }
	.row.current .lead { color: var(--accent-line); }
	.row.current .lead :global(svg) { opacity: 1; }
	.row.done .lead, .row.busy .lead { color: var(--accent-line); }
	.reddot { width: calc(var(--space-1) * 2); height: calc(var(--space-1) * 2); border-radius: var(--radius-circle); background: var(--red); }
	.done-check path { stroke-dasharray: 1; stroke-dashoffset: 1; animation: draw-check var(--dur-tight) var(--ease-snap) forwards; }
	.spinner { animation: spin var(--dur-hero-plus) linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@keyframes draw-check { to { stroke-dashoffset: 0; } }
	@media (prefers-reduced-motion: reduce) { .spinner { animation: none; } .done-check path { animation: none; stroke-dashoffset: 0; } }

	.copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
	.lbl { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.details { display: grid; grid-template-rows: 0fr; opacity: 0; transition: grid-template-rows calc(var(--dur-standard) * 0.7) var(--ease-snap), opacity calc(var(--dur-standard) * 0.7) var(--ease-snap); }
	.details.open { grid-template-rows: 1fr; opacity: 1; transition-duration: var(--dur-standard); transition-timing-function: var(--ease-entrance); }
	.details-inner { min-height: 0; overflow: hidden; display: flex; flex-direction: column; gap: 1px; }
	.meta { color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.2; }
	.row.error .meta { color: var(--red-text); }
	.hair { display: block; height: calc(var(--space-1) / 2); margin-top: var(--space-1); border-radius: var(--radius-pill); background: var(--border); overflow: hidden; }
	.hair i { display: block; height: 100%; background: var(--accent-line); border-radius: inherit; transition: width var(--dur-standard) var(--ease-material); }
	@media (prefers-reduced-motion: reduce) { .details { transition: opacity var(--dur-tight) var(--ease-snap); } .details.open { grid-template-rows: 1fr; } .hair i { transition: none; } }
</style>
