<script lang="ts">
	import { Button, Field, IconButton, Modal } from '$lib/components/ui';
	import { providers, type ProviderEntry } from '$lib/stores/providers';
	import { addNetworkServer, parseModelIds } from '$lib/stores/networkServers';
	import { loadHostGroups } from '$lib/stores/providersV7';

	interface Props {
		onClose: () => void;
		onSelectCloud: (p: ProviderEntry) => void;
	}

	const { onClose, onSelectCloud }: Props = $props();

	type Mode = 'menu' | 'remote' | 'endpoint';
	let mode = $state<Mode>('menu');

	const cloudUnconfigured = $derived(($providers).filter(p => p.type === 'cloud' && !p.has_key));

	let busy = $state(false);
	let actionError = $state('');
	let remoteHost = $state('');
	let remotePort = $state('');
	let remoteName = $state('');
	let endpointUrl = $state('');
	let endpointDialect = $state<'openai' | 'anthropic'>('openai');
	let endpointKey = $state('');
	let endpointModels = $state('');
	let endpointName = $state('');
	let showEndpointOptions = $state(false);

	const defaultPort = 8000;

	async function handleAddRemote() {
		actionError = '';
		const host = remoteHost.trim();
		if (!host) { actionError = 'Enter the computer address.'; return; }
		const port = remotePort.trim() ? parseInt(remotePort.trim(), 10) : defaultPort;
		const name = remoteName.trim() || host;
		busy = true;
		const r = await addNetworkServer(name, host, port);
		busy = false;
		if (!r.ok) {
			actionError = r.message ?? 'Failed to reach host';
			return;
		}
		await loadHostGroups();
		onClose();
	}

	async function handleAddEndpoint() {
		actionError = '';
		const base = endpointUrl.trim();
		if (!/^https?:\/\/[\w.-]+/i.test(base)) {
			actionError = 'Enter the full web address, including https://';
			return;
		}
		// The URL is the endpoint on this path: host and port are not sent, and the
		// backend derives the port from the URL's own scheme. The host part is kept
		// only as a display name when the user did not type one.
		const host = base.split('/')[2] ?? base;
		busy = true;
		const r = await addNetworkServer(endpointName.trim() || host, '', 0, {
			base_url: base,
			dialect: endpointDialect,
			api_key: endpointKey.trim() || undefined,
			model_ids: parseModelIds(endpointModels),
		});
		busy = false;
		if (!r.ok) {
			actionError = r.message ?? 'Failed to reach that endpoint';
			return;
		}
		await loadHostGroups();
		onClose();
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && mode !== 'menu') {
			e.stopPropagation();
			mode = 'menu';
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<Modal size="sm" closeOnEscape={mode === 'menu'} onClose={onClose}>
	{#snippet head()}
		<header class="head">
			<div class="title">
				{#if mode === 'remote'}
					<IconButton ariaLabel="Back" onclick={() => mode = 'menu'}>
						<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>
					</IconButton>
				{/if}
				<span>{mode === 'menu' ? 'Add provider' : mode === 'remote' ? 'oMLX on another Mac' : 'Other provider by address'}</span>
			</div>
			<IconButton ariaLabel="Close" onclick={onClose}>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6 18 18M18 6 6 18"/></svg>
			</IconButton>
		</header>
	{/snippet}


	{#if mode === 'menu'}
		{#if cloudUnconfigured.length > 0}
			<div class="grp">Cloud</div>
			{#each cloudUnconfigured as p (p.name)}
				<button type="button" class="ai press-tint" onclick={() => { onSelectCloud(p); onClose(); }}>
					<span class="ai-icon">
						<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 7a6 6 0 0 1-8 5.7L8 17l-2 2-3-1 1-3 9.5-9.5z"/></svg>
					</span>
					<span class="ai-text">
						<span class="ai-name">{p.label}</span>
						<span class="ai-desc">Cloud API · enter your key</span>
					</span>
					<svg class="arrow" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>
				</button>
			{/each}
		{/if}

		<div class="grp">Other ways to connect</div>
		<button type="button" class="ai press-tint" onclick={() => mode = 'remote'}>
			<span class="ai-icon" style="color: var(--sky);">
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>
			</span>
			<span class="ai-text">
				<span class="ai-name">oMLX on another Mac</span>
				<span class="ai-desc">Use the local models oMLX runs on another Mac</span>
			</span>
			<svg class="arrow" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>
		</button>

		<button type="button" class="ai press-tint" onclick={() => mode = 'endpoint'}>
			<span class="ai-icon" style="color: var(--sky);">
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>
			</span>
			<span class="ai-text">
				<span class="ai-name">Other provider by address</span>
				<span class="ai-desc">Add a provider with its web address</span>
			</span>
			<svg class="arrow" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>
		</button>

		{#if cloudUnconfigured.length === 0}
			<div class="empty">All known providers are configured. You can still add oMLX on another Mac, or another provider by address.</div>
		{/if}
	{:else if mode === 'endpoint'}
		<p class="hint">Add a provider using the full web address it gives you.</p>

		<Field label="Web address" hint="Use the address exactly as your provider gives it.">
			<input placeholder="https://host.example.com/compatible-mode/v1" bind:value={endpointUrl} />
		</Field>
		<Field label="API key" hint="Stored on this computer only. Never shown again.">
			<input type="password" placeholder="(leave empty when the web address needs no key)" bind:value={endpointKey} />
		</Field>
		<button type="button" class="more-options" aria-expanded={showEndpointOptions} onclick={() => showEndpointOptions = !showEndpointOptions}>
			{showEndpointOptions ? 'Fewer options' : 'More options'}
		</button>
		{#if showEndpointOptions}
			<Field label="Connection type">
				<select bind:value={endpointDialect}>
					<option value="openai">OpenAI-compatible</option>
					<option value="anthropic">Anthropic</option>
				</select>
			</Field>
			<Field label="Model names" hint="Only needed when the provider does not list its models. One per line or comma-separated.">
				<textarea rows="2" placeholder="model-one, model-two" bind:value={endpointModels}></textarea>
			</Field>
			<Field label="Name">
				<input placeholder="(optional — uses the address)" bind:value={endpointName} />
			</Field>
		{/if}
		{#if actionError}<p class="error-text">{actionError}</p>{/if}

		<div class="actions">
			<Button variant="ghost" disabled={busy} onclick={() => mode = 'menu'}>Back</Button>
			<Button
				variant="primary"
				disabled={busy || !endpointUrl.trim()}
				loading={busy}
				onclick={handleAddEndpoint}
			>
				{busy ? 'Testing…' : 'Test & add'}
			</Button>
		</div>
	{:else}
		<p class="hint">Add the local models oMLX already runs on another Mac.</p>

		<Field label="Computer address">
			<input placeholder="100.64.1.5  ·  macbook-m4.local" bind:value={remoteHost} />
		</Field>
		<Field label="Port" hint="leave empty for default ({defaultPort})">
			<input placeholder={`${defaultPort}`} bind:value={remotePort} />
		</Field>
		<Field label="Name">
			<input placeholder="(optional — uses the address)" bind:value={remoteName} />
		</Field>
		{#if actionError}<p class="error-text">{actionError}</p>{/if}

		<div class="actions">
			<Button variant="ghost" disabled={busy} onclick={() => mode = 'menu'}>Back</Button>
			<Button
				variant="primary"
				disabled={busy || !remoteHost.trim()}
				loading={busy}
				onclick={handleAddRemote}
			>
				{busy ? 'Testing…' : 'Test & add'}
			</Button>
		</div>
	{/if}
</Modal>

<style>
	.head {
		display: flex; align-items: center; gap: 12px;
		padding: 18px 22px;
	}
	.title {
		flex: 1;
		display: flex; align-items: center; gap: 8px;
		font-size: var(--text-body); font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--text-primary);
	}

	.grp {
		font-size: var(--text-caption); font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
		color: var(--text-tertiary);
		padding: 12px 4px 6px;
	}
	.grp:first-child { padding-top: 0; }

	.ai {
		display: grid;
		grid-template-columns: 32px 1fr auto;
		gap: 12px; align-items: center;
		padding: 10px 12px;
		border-radius: var(--radius-xs);
		background: transparent; border: 0;
		color: inherit; font-family: inherit;
		text-align: left;
		width: 100%;
		cursor: pointer;
		transition: all var(--dur-tight) var(--ease-snap);
	}
	.ai:hover:not(:disabled) { background: var(--bg-tertiary); }
	.ai:disabled { opacity: 0.5; cursor: not-allowed; }
	.ai-icon {
		display: inline-flex; align-items: center; justify-content: center;
		width: 32px; height: 32px;
		border-radius: var(--radius-xs);
		background: var(--bg-tertiary);
		border: 1px solid var(--border-subtle);
		color: var(--text-tertiary);
	}
	.ai-text { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
	.ai-name { font-size: var(--text-subhead); font-weight: 500; color: var(--text-primary); }
	.ai-desc { font-size: var(--text-footnote); color: var(--text-tertiary); }
	.arrow { color: var(--text-tertiary); flex-shrink: 0; }
	.ai:hover:not(:disabled) .arrow { color: var(--text-secondary); }

	.empty {
		font-size: var(--text-subhead); color: var(--text-tertiary);
		padding: 20px 16px; text-align: center;
		line-height: 1.6;
	}

	.hint {
		font-size: var(--text-subhead); color: var(--text-tertiary);
		line-height: 1.6;
		padding: 14px 16px;
		background: var(--bg-tertiary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		margin: 0 0 10px;
	}

	.more-options {
		align-self: flex-start;
		border: 0;
		padding: 0;
		background: transparent;
		color: var(--accent-text);
		font: inherit;
		font-size: var(--text-footnote);
		cursor: pointer;
	}
	.error-text { margin: var(--space-2) 0 0; }
	.actions {
		display: flex; gap: 8px; margin-top: 18px; align-items: center; justify-content: flex-end;
	}

</style>
