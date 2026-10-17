<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		ariaLabel: string;
		variant?: 'default' | 'ghost' | 'danger' | 'shell';
		size?: 'sm' | 'md' | 'lg';
		disabled?: boolean;
		title?: string;
		type?: 'button' | 'submit';
		class?: string;
		style?: string;
		onclick?: (e: MouseEvent) => void;
		children: Snippet;
	}

	const {
		ariaLabel, variant = 'default', size = 'md',
		disabled = false, title, type = 'button',
		class: extraClass = '', style,
		onclick, children,
	}: Props = $props();
</script>

<button
	{type}
	class="ico-btn {variant} {size} {extraClass}"
	{style}
	aria-label={ariaLabel}
	{title}
	{disabled}
	{onclick}
>
	{@render children()}
</button>

<style>
	.ico-btn {
		position: relative;
		display: inline-flex; align-items: center; justify-content: center;
		border-radius: var(--radius-xs);
		background: transparent; border: 0;
		cursor: pointer;
		font-family: inherit;
		transition: all var(--dur-tight) var(--ease-snap);
	}
	.ico-btn:disabled { opacity: 0.5; cursor: not-allowed; }
	@media (max-width: 768px) {
		.ico-btn.sm::after, .ico-btn.md::after {
			content: ''; position: absolute; border-radius: inherit;
		}
		.ico-btn.sm::after { inset: -10px; }
		.ico-btn.md::after { inset: -6px; }
	}

	.ico-btn.md { width: 32px; height: 32px; }
	.ico-btn.sm { width: 24px; height: 24px; }
	.ico-btn.lg { width: 36px; height: 36px; }

	.ico-btn.default { color: var(--text-tertiary); }
	.ico-btn.default:hover:not(:disabled) {
		background: var(--bg-hover); color: var(--text-primary);
	}

	.ico-btn.ghost { color: var(--text-tertiary); }
	.ico-btn.ghost:hover:not(:disabled) { color: var(--text-primary); }

	.ico-btn.danger { color: var(--text-tertiary); }
	.ico-btn.danger:hover:not(:disabled) {
		background: var(--red-bg); color: var(--red-text);
	}

	.ico-btn.shell {
		color: var(--text-secondary);
		background: color-mix(in srgb, var(--bg-tertiary) 60%, transparent);
		border: 1px solid var(--border);
		border-radius: var(--radius-circle);
		box-shadow: var(--glass-rim), 0 2px 8px rgba(0, 0, 0, 0.15);
	}
	.ico-btn.shell:hover:not(:disabled) {
		background: var(--bg-hover);
		color: var(--text-primary);
		border-color: var(--border);
	}

	@media (max-width: 768px) {
		.ico-btn.lg { width: 44px; height: 44px; }
	}
</style>
