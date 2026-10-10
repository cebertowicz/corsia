<script lang="ts">
	import { fly } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import type { Snippet } from 'svelte';
	import { providerEnabled, type HostProvider } from '$lib/stores/providersV7';

	interface Props {
		provider: HostProvider;
		expanded?: boolean;
		hasLoaded?: boolean;
		accentVar?: string;
		onToggleEnable?: () => void;
		onSettings?: () => void;
		onToggleExpand?: () => void;
		body?: Snippet;
	}

	const {
		provider,
		expanded = false,
		hasLoaded = false,
		accentVar = 'var(--violet)',
		onToggleEnable,
		onSettings,
		onToggleExpand,
		body,
	}: Props = $props();

	const enabled = $derived(providerEnabled(provider));
	// Harness cards have account settings, but the provider-wide enable hold is not
	// implemented yet. Keep its switch hidden until that state has a real owner.
	const configurable = $derived(provider.type !== 'harness');
	const discoveryWarning = $derived.by(() => {
		const discovery = provider.server?.metadata?.discovery;
		if (provider.server?.kind !== 'external' || !discovery) return '';
		if (discovery.error) return `Model discovery failed: ${discovery.error}`;
		if (discovery.models === 0) return 'Reached, but it listed no models';
		if (discovery.reasoning_levels_error) {
			return `Reasoning levels not updated: ${discovery.reasoning_levels_error}`;
		}
		return '';
	});

</script>

<div
	class="pr"
	data-provider-card={provider.name}
	class:expanded
	class:has-loaded={hasLoaded}
	style="--accent: {accentVar}"
>
	<div class="pr-head" class:configurable>
		<button
			type="button"
			class="pr-expand"
			aria-expanded={expanded}
			aria-label="{provider.label} — {expanded ? 'collapse' : 'expand'}"
			onclick={() => onToggleExpand?.()}
		>
			<span class="dot-wrap"><span class="dot {provider.statusDot}"></span></span>
			<span class="pr-info"><span class="name-row"><span class="name">{provider.label}</span></span><span class="meta">{provider.subline}</span></span>
			<span class="ico-btn sm chev" aria-hidden="true"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span>
		</button>

		<button
			type="button"
			class="ico-btn"
			aria-label="Settings"
			onclick={() => onSettings?.()}
		>
			<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
				<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7 7 0 0 0-.1-1.2l2.1-1.6-2-3.4-2.5.9a7 7 0 0 0-2-1.2L14.5 2h-5l-.5 2.5a7 7 0 0 0-2 1.2l-2.5-.9-2 3.4L4.6 9.8a7 7 0 0 0 0 2.4l-2 1.6 2 3.4 2.5-.9a7 7 0 0 0 2 1.2L9.5 22h5l.5-2.5a7 7 0 0 0 2-1.2l2.5.9 2-3.4-2.1-1.6c0-.4.1-.8.1-1.2Z"/>
			</svg>
		</button>

		{#if configurable}
			<button
				type="button"
				class="tg"
				class:on={enabled}
				aria-label={enabled ? 'Disable' : 'Enable'}
				onclick={() => onToggleEnable?.()}
			></button>
		{/if}
	</div>

	{#if discoveryWarning}
		<p class="discovery-warning" role="status">{discoveryWarning}</p>
	{/if}

	{#if expanded}
		<div class="pr-body" transition:fly={{ y: -4, duration: 160, easing: quintOut }}>
			{#if body}{@render body()}{/if}
		</div>
	{/if}
</div>

<style>
	.pr {
		background: var(--bg-secondary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius);
		transition: all var(--dur-tight) var(--ease-snap);
		overflow: hidden;
		position: relative;
	}
	.pr:hover { border-color: var(--border); }
	.pr.expanded {
		background: var(--bg-tertiary);
		box-shadow: 0 1px 0 rgba(255,255,255,0.02) inset, 0 1px 2px rgba(0,0,0,0.3);
	}

	.pr-head {
		display: grid;
		grid-template-columns: 22px minmax(0, 1fr) auto 28px;
		align-items: center;
		gap: 12px;
		padding: 12px 14px;
	}
	.pr-head.configurable {
		grid-template-columns: 22px minmax(0, 1fr) auto auto 28px;
	}
	.pr-expand {
		grid-column: 1 / -1;
		grid-row: 1;
		display: grid;
		grid-template-columns: subgrid;
		align-items: center;
		min-width: 0;
		padding: 0;
		border: 0;
		color: inherit;
		background: transparent;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.pr-expand:focus { outline: none; }
	.pr-expand:focus-visible {
		outline: 2px solid var(--violet);
		outline-offset: -2px;
	}
	.pr-expand .chev { grid-column: -2; cursor: inherit; }
	.pr-head > .ico-btn { grid-column: 3; grid-row: 1; z-index: 1; }
	.pr-head > .tg { grid-column: 4; grid-row: 1; z-index: 1; }

	.dot-wrap { display: flex; justify-content: center; }
	.dot {
		width: 8px; height: 8px; border-radius: var(--radius-circle);
		flex-shrink: 0;
		position: relative;
	}
	.dot::after {
		content: '';
		position: absolute; inset: -4px;
		border-radius: var(--radius-circle); opacity: 0.18;
	}
	.dot.online   { background: var(--green); box-shadow: 0 0 8px color-mix(in srgb, var(--green) 50%, transparent); }
	.dot.online::after { background: var(--green); animation: pulse 2.4s ease-out infinite; }
	.dot.idle     { background: var(--amber); box-shadow: 0 0 8px color-mix(in srgb, var(--amber) 40%, transparent); }
	.dot.error    { background: var(--red); box-shadow: 0 0 8px color-mix(in srgb, var(--red) 40%, transparent); }
	.dot.disabled { background: var(--text-tertiary); }

	@keyframes pulse {
		0%   { transform: scale(1);   opacity: 0.4; }
		100% { transform: scale(2.6); opacity: 0; }
	}

	.pr-info { min-width: 0; }
	.pr-info .name-row {
		display: flex; align-items: center; gap: 10px;
		margin-bottom: 4px;
	}
	.name {
		font-size: var(--text-callout);
		font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--text-primary);
	}

	.meta {
		font-size: var(--text-subhead); color: var(--text-tertiary);
		font-variant-numeric: tabular-nums;
	}
	.discovery-warning {
		margin: -4px 14px 10px 48px;
		font-size: var(--text-footnote);
		color: var(--amber);
		line-height: 1.35;
	}

	.ico-btn {
		display: inline-flex; align-items: center; justify-content: center;
		width: 32px; height: 32px;
		border-radius: var(--radius-xs);
		color: var(--text-tertiary);
		transition: all var(--dur-tight) var(--ease-snap);
		background: transparent;
		border: 0;
		cursor: pointer;
	}
	.ico-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
	.ico-btn.sm { width: 28px; height: 28px; }
	.ico-btn.chev { color: var(--text-tertiary); cursor: default; }
	.ico-btn.chev :global(svg) { transition: transform var(--dur-standard) var(--ease-snap); }
	.pr.expanded .pr-expand .ico-btn.chev :global(svg) { transform: rotate(180deg); }

	.tg {
		--w: 36px; --h: 20px;
		width: var(--w); height: var(--h);
		background: var(--bg-tertiary);
		border: 1px solid var(--border);
		border-radius: var(--radius-pill);
		position: relative;
		transition: all var(--dur-tight) var(--ease-snap);
		flex-shrink: 0;
		cursor: pointer;
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.3);
	}
	.tg::after {
		content: '';
		position: absolute;
		width: 14px; height: 14px;
		border-radius: var(--radius-circle);
		background: linear-gradient(180deg, #d8d8e0, #b8b8c0);
		top: 2px; left: 2px;
		transition: all var(--dur-tight) var(--ease-snap);
		box-shadow: 0 1px 2px rgba(0,0,0,0.4);
	}
	.tg.on {
		background: linear-gradient(180deg, var(--violet-2), var(--violet));
		border-color: var(--violet);
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.2), 0 0 12px color-mix(in srgb, var(--violet) 30%, transparent);
	}
	.tg.on::after {
		left: calc(var(--w) - 18px);
		background: linear-gradient(180deg, #ffffff, #f0f0f5);
	}

	.pr-body {
		border-top: 1px solid var(--border-subtle);
		background: var(--bg-secondary);
	}

	@media (max-width: 480px) {
		.pr-head {
			grid-template-columns: 18px minmax(0, 1fr) 28px 20px;
			gap: 8px;
			padding: 10px 12px;
		}
		.pr-head.configurable { grid-template-columns: 18px minmax(0, 1fr) 28px 36px 20px; }
		.pr-info .name-row { gap: 6px; margin-bottom: 2px; }
		.name { font-size: var(--text-subhead); }
		.meta { font-size: var(--text-footnote); }
		.ico-btn { width: 28px; height: 28px; }
		.tg { --w: 32px; --h: 18px; }
		.tg::after { width: 12px; height: 12px; top: 2px; left: 2px; }
		.tg.on::after { left: calc(var(--w) - 16px); }
	}
</style>
