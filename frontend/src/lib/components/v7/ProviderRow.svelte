<script lang="ts">
	import { fly } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import type { Snippet } from 'svelte';
	import { providerEnabled, type HostProvider } from '$lib/stores/providersV7';
	import { Toggle } from '$lib/components/ui';

	interface Props {
		provider: HostProvider;
		expanded?: boolean;
		hasLoaded?: boolean;
		onToggleEnable?: () => void;
		onSettings?: () => void;
		onToggleExpand?: () => void;
		body?: Snippet;
		/** The local setup row replaces settings and enable controls with one existing setup action or its refusal. */
		setupLabel?: string;
		onSetup?: () => void;
	}

	const {
		provider,
		expanded = false,
		hasLoaded = false,
		onToggleEnable,
		onSettings,
		onToggleExpand,
		body,
		setupLabel,
		onSetup,
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
	data-server-card={provider.server?.id}
	class:expanded
	class:has-loaded={hasLoaded}
>
	<div class="pr-head" class:setup={Boolean(setupLabel)}>
		{#if setupLabel}
			<div class="pr-setup-summary">
				<span class="dot-wrap"><span class="dot {provider.statusDot}"></span></span>
				<span class="pr-info"><span class="name-row"><span class="name">{provider.label}</span></span><span class="meta">{provider.subline}</span></span>
			</div>
		{:else}
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
		{/if}

		{#if setupLabel}
			{#if onSetup}
				<button type="button" class="setup-action" onclick={onSetup}>{setupLabel}</button>
			{:else}
				<span class="setup-refusal">{setupLabel}</span>
			{/if}
		{:else}
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
				<Toggle checked={enabled} ariaLabel={enabled ? 'Disable' : 'Enable'} onchange={() => onToggleEnable?.()} />
			{/if}
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
	/* A Settings list row: flat on the pane with a hover wash for a pointer, no box at rest or
	   when open. The status dot takes the project-mark column so names line up with other rows. */
	.pr { position: relative; }

	/* The switch column keeps the md Toggle's width on every row, so a row without a switch
	   (an agent CLI) keeps its gear in the same column as the rows above it. */
	.pr-head {
		display: grid;
		grid-template-columns: var(--project-mark-size) minmax(0, 1fr) auto 34px 28px;
		align-items: center;
		gap: var(--space-3);
		min-height: var(--list-row-min-height);
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-sm);
	}
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last row tapped. */
	@media (hover: hover) {
		.pr-head:hover { background: var(--bg-hover); }
	}
	.pr-head.setup { grid-template-columns: auto minmax(0, 1fr) auto; }
	.pr-expand, .pr-setup-summary {
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
	.pr-setup-summary { cursor: default; }
	.pr-expand:focus { outline: none; }
	.pr-expand:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
	}
	.pr-expand .chev { grid-column: -2; cursor: inherit; }
	/* The chevron box hugs its glyph at the column's end, so the glyph ends on the content edge with every other row's trailing control. */
	.pr-expand .ico-btn.chev { width: auto; justify-self: end; }
	.pr-head > .ico-btn { grid-column: 3; grid-row: 1; z-index: 1; }
	.pr-head > :global(.tg) { grid-column: 4; grid-row: 1; z-index: 1; }
	.setup-action, .setup-refusal { grid-column: 3; grid-row: 1; z-index: 1; }
	.setup-action {
		padding: var(--space-2);
		border: 0; background: transparent; color: var(--accent-text);
		font: inherit; font-size: var(--text-footnote); cursor: pointer; white-space: nowrap;
	}
	.setup-action:hover { color: var(--text-primary); }
	.setup-action:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.setup-refusal { max-width: 16rem; color: var(--text-tertiary); font-size: var(--text-footnote); text-align: right; }

	.dot-wrap { display: flex; justify-content: center; }
	.dot {
		width: 8px; height: 8px; border-radius: var(--radius-circle);
		flex-shrink: 0;
		position: relative;
	}
	/* Status is a plain dot in its colour: no glow, no pulse. */
	.dot.online   { background: var(--green); }
	.dot.idle     { background: var(--amber); }
	.dot.error    { background: var(--red); }
	.dot.disabled { background: var(--text-tertiary); }

	.pr-info { min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); }
	.pr-info .name-row { display: flex; align-items: center; gap: var(--space-2); }
	.name { font-size: var(--text-subhead); color: var(--text-primary); }

	.meta {
		font-size: var(--text-caption); color: var(--text-tertiary);
		font-variant-numeric: tabular-nums;
	}
	/* A second status line, indented to the name column. */
	.discovery-warning {
		margin: 0 var(--space-2) var(--space-1) calc(var(--space-2) + var(--project-mark-size) + var(--space-3));
		font-size: var(--text-caption);
		color: var(--amber-text);
		line-height: 1.45;
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

	@media (max-width: 480px) {
		.pr-head { grid-template-columns: var(--project-mark-size) minmax(0, 1fr) 28px 34px 20px; }
		.ico-btn { width: 28px; height: 28px; }
	}
</style>
