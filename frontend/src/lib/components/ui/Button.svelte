<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
		size?: 'sm' | 'md';
		type?: 'button' | 'submit';
		disabled?: boolean;
		loading?: boolean;
		ariaLabel?: string;
		title?: string;
		onclick?: (e: MouseEvent) => void;
		children: Snippet;
	}

	const {
		variant = 'secondary', size = 'md', type = 'button',
		disabled = false, loading = false,
		ariaLabel, title,
		onclick, children,
	}: Props = $props();

	const isDisabled = $derived(disabled || loading);
</script>

<button
	{type}
	class="btn {variant} {size}"
	class:loading
	disabled={isDisabled}
	aria-label={ariaLabel}
	{title}
	{onclick}
>
	{#if loading}
		<span class="spinner" aria-hidden="true"></span>
	{/if}
	{@render children()}
</button>

<style>
	.btn {
		position: relative;
		display: inline-flex; align-items: center; justify-content: center; gap: 6px;
		border-radius: var(--radius-sm);
		font-family: inherit;
		font-weight: 500;
		cursor: pointer;
		transition: all var(--dur-tight) var(--ease-snap);
		white-space: nowrap;
		border: 1px solid;
	}
	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn.loading { cursor: wait; }
	@media (max-width: 768px) {
		.btn::after { content: ''; position: absolute; border-radius: inherit; }
		.btn.md::after { inset: -6px; }
		.btn.sm::after { inset: -8px; }
	}

	.btn.md { height: 32px; padding: 0 13px; font-size: var(--text-subhead); }
	.btn.sm { height: 28px; padding: 0 10px; font-size: var(--text-footnote); }

	.btn.secondary {
		background: var(--bg-secondary);
		border-color: var(--border);
		color: var(--text-secondary);
	}
	.btn.secondary:hover:not(:disabled) {
		background: var(--violet-bg);
		border-color: var(--violet-line);
		color: var(--violet-text, var(--violet));
	}

	.btn.primary {
		background: var(--violet-bg);
		border-color: var(--violet-line);
		color: var(--violet-text, var(--violet));
	}
	.btn.primary:hover:not(:disabled) {
		background: color-mix(in srgb, var(--violet) 18%, transparent);
		border-color: var(--violet);
	}

	.btn.danger {
		background: var(--red-bg);
		border-color: color-mix(in srgb, var(--red) 30%, transparent);
		color: var(--red);
	}
	.btn.danger:hover:not(:disabled) {
		background: color-mix(in srgb, var(--red) 18%, transparent);
		border-color: var(--red);
	}

	.btn.ghost {
		background: transparent;
		border-color: transparent;
		color: var(--text-tertiary);
	}
	.btn.ghost:hover:not(:disabled) {
		background: var(--bg-tertiary);
		color: var(--text-primary);
	}

	.spinner {
		display: inline-block;
		width: 12px; height: 12px;
		border: 2px solid currentColor;
		border-right-color: transparent;
		border-radius: var(--radius-circle);
		animation: spin var(--dur-hero-plus) linear infinite;
	}
	@keyframes spin {
		to { transform: rotate(360deg); }
	}
</style>
