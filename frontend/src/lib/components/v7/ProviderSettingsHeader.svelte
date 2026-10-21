<script lang="ts">
	import { IconButton } from '$lib/components/ui';
	import type { HostProvider } from '$lib/stores/providersV7';
	import type { ProviderSettingsTab } from './providerSettingsTypes';

	interface Props {
		provider: HostProvider;
		tabs: { id: ProviderSettingsTab; label: string }[];
		activeTab: ProviderSettingsTab;
		onClose: () => void;
	}

	let {
		provider,
		tabs,
		activeTab = $bindable('overview'),
		onClose,
	}: Props = $props();

	// Provider protocols can share a label; the registered name identifies this row.
	const title = $derived(
		provider.server?.instance_name
			? provider.server.instance_name
			: provider.label,
	);
	// Grouping a loopback endpoint with this computer does not establish that its
	// models are local. The backend supplies that fact, or the person marks it private.
	const showTypeTag = $derived(
		provider.type !== 'local' || !provider.server || provider.server.on_this_mac === true || provider.server.private === true,
	);
</script>

<header class="head">
	<div class="title">
		<span class="dot {provider.statusDot}"></span>
		<span>{title}</span>
		{#if showTypeTag}<span class="type-tag {provider.type}">{provider.type === 'cloud' ? 'API' : provider.type}</span>{/if}
	</div>
	<IconButton ariaLabel="Close" onclick={onClose}>
		<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6 18 18M18 6 6 18"/></svg>
	</IconButton>
</header>
<nav class="tabs">
	{#each tabs as t (t.id)}
		<button type="button" class="tab" class:active={activeTab === t.id} onclick={() => activeTab = t.id}>{t.label}</button>
	{/each}
</nav>

<style>
	.head {
		display: flex; align-items: center; gap: 12px;
		padding: 18px 22px;
	}
	.title {
		flex: 1;
		display: flex; align-items: center; gap: 10px;
		font-size: var(--text-body); font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--text-primary);
	}

	.dot {
		width: 8px; height: 8px; border-radius: var(--radius-circle); flex-shrink: 0;
	}
	.dot.online   { background: var(--green); box-shadow: 0 0 8px color-mix(in srgb, var(--green) 50%, transparent); }
	.dot.idle     { background: var(--amber); }
	.dot.error    { background: var(--red); }
	.dot.disabled { background: var(--text-tertiary); }

	.type-tag {
		display: inline-flex;
		font-size: var(--text-caption); font-weight: 600; letter-spacing: 0.04em;
		padding: 2px 8px;
		border-radius: var(--radius-xs);
		background: var(--bg-tertiary);
		border: 1px solid var(--border);
		color: var(--text-tertiary);
		text-transform: uppercase;
	}
	.type-tag.local   { background: var(--teal-bg); border-color: var(--teal-line); color: var(--teal); }
	.type-tag.network { background: var(--sky-bg);  border-color: var(--sky-line);  color: var(--sky); }

	.tabs {
		display: flex;
		padding: 0 18px;
		border-bottom: 1px solid var(--border-subtle);
		gap: 2px;
		overflow-x: auto;
	}
	.tab {
		padding: 12px 14px;
		font-size: var(--text-subhead); font-weight: 500;
		color: var(--text-tertiary);
		background: transparent; border: 0; border-bottom: 2px solid transparent;
		font-family: inherit; cursor: pointer;
		transition: all var(--dur-tight) var(--ease-snap);
		white-space: nowrap;
	}
	.tab:hover { color: var(--text-primary); }
	.tab.active { color: var(--accent-text, var(--accent)); border-bottom-color: var(--accent); }

	@media (max-width: 480px) {
		.head { padding: 14px 16px; }
		.tab { padding: 10px 10px; font-size: var(--text-footnote); }
	}
</style>
