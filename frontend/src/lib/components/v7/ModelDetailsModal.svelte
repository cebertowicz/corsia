<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import type { ProviderModel } from '$lib/stores/providerModels';
	import { IconButton, Modal, Section, StatePill } from '$lib/components/ui';
	import CheckpointWindowEditor from './CheckpointWindowEditor.svelte';
	import ImageInputEditor from './ImageInputEditor.svelte';
	import ModelLabelEditor from './ModelLabelEditor.svelte';

	interface Props {
		model: ProviderModel;
		providerLabel: string;
		providerName: string;
		serverId?: string;
		external?: boolean;
		/** Claude Code and Codex rows: the user may rename the model in the picker. */
		labelEditable?: boolean;
		omlxAdminUrl?: string;
		onChanged?: () => void;
		onClose: () => void;
	}

	const { model, providerLabel, providerName, serverId, external = false, labelEditable = false, omlxAdminUrl, onChanged, onClose }: Props = $props();

	const showsCheckpointing = $derived(!!model.dbId && (external || labelEditable || providerName === 'omlx'));

	const capLabels: Record<string, string> = {
		vision: 'Vision · accepts image inputs',
		reasoning: 'Reasoning · supports thinking tokens',
		tools: 'Tools · function/tool calling',
	};

	type CacheEntry = {
		model_id?: string;
		thread_id?: string;
		tokens?: number;
		count?: number;
		max?: number;
		total_tokens?: number;
		active_slots?: number;
	};
	let cacheStats = $state<CacheEntry[] | null>(null);
	let cacheLoading = $state(false);

	type RuntimeInfo = {
		loaded?: boolean;
		pinned?: boolean | null;
		last_access?: number | null;
		engine_type?: string | null;
		thinking_default?: boolean | null;
		preserve_thinking_default?: boolean | null;
	};
	let runtime = $state<RuntimeInfo | null>(null);
	let runtimeLoading = $state(false);
	let pinSaving = $state(false);
	let pinError = $state<string | null>(null);

	const showsServerState = $derived(
		providerName === 'omlx' && !!serverId && !!model.id,
	);

	async function loadRuntime() {
		if (!showsServerState) return;
		runtimeLoading = true;
		try {
			const t = get(token) || '';
			const res = await fetch(
				`/api/settings/network-servers/${serverId}/models/${encodeURIComponent(model.id)}/runtime-info?token=${t}`,
			);
			if (res.ok) {
				runtime = await res.json();
			}
		} catch {
		} finally {
			runtimeLoading = false;
		}
	}

	async function togglePinned() {
		if (!showsServerState || pinSaving) return;
		pinError = null;
		pinSaving = true;
		const next = !runtime?.pinned;
		try {
			const t = get(token) || '';
			const res = await fetch(
				`/api/settings/network-servers/${serverId}/models/${encodeURIComponent(model.id)}/settings?token=${t}`,
				{
					method: 'PUT',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ pinned: next }),
				},
			);
			if (!res.ok) {
				const errBody = await res.json().catch(() => ({}));
				pinError = errBody.detail ?? `HTTP ${res.status}`;
			} else {
				await loadRuntime();
			}
		} catch (e: any) {
			pinError = e?.message ?? String(e);
		} finally {
			pinSaving = false;
		}
	}

	onMount(async () => {
		if (!serverId) return;
		cacheLoading = true;
		const t = get(token) || '';
		try {
			const res = await fetch(`/api/settings/network-servers/${serverId}/cache/status?token=${t}`);
			if (res.ok) {
				const data = await res.json();
				cacheStats = (data.caches || []).filter((c: CacheEntry) =>
					!c.model_id || c.model_id === model.id,
				);
			}
		} catch {
		} finally {
			cacheLoading = false;
		}
		await loadRuntime();
	});

	const cacheSummary = $derived.by(() => {
		if (!cacheStats || cacheStats.length === 0) return null;
		const agg = cacheStats[0];
		if (agg.count !== undefined && agg.max !== undefined) return agg;
		const totalTokens = cacheStats.reduce((s, c) => s + (c.tokens ?? c.total_tokens ?? 0), 0);
		return { count: cacheStats.length, total_tokens: totalTokens };
	});

	const pillState = $derived(
		model.state === 'cache_warming' || model.state === 'queued' ? 'loading'
		: model.state === 'default' ? 'available'
		: model.state,
	);
	// Claude Code and Codex rows are never loaded: on is the user's show choice, not picker delivery.
	const pillLabel = $derived(labelEditable ? (model.state === 'loaded' ? 'Shown' : 'Hidden') : undefined);
</script>

<Modal size="sm" onClose={onClose}>
	{#snippet head()}
		<header class="head">
			<div class="title">
				<span class="model-name">{model.name}</span>
				<StatePill state={pillState} label={pillLabel} />
			</div>
			<IconButton ariaLabel="Close" onclick={onClose}>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6 18 18M18 6 6 18"/></svg>
			</IconButton>
		</header>
	{/snippet}

	<div class="row"><span class="l">Provider</span><span class="v">{providerLabel}</span></div>
	<div class="row"><span class="l">Model id</span><span class="v"><code>{model.id}</code></span></div>
	{#if model.providerContextLength ?? model.contextLength}
		<div class="row"><span class="l">Context</span><span class="v">{(model.providerContextLength ?? model.contextLength)?.toLocaleString()} tokens</span></div>
	{/if}
	{#if showsCheckpointing && model.providerContextLength && model.contextLength && model.providerContextLength !== model.contextLength}
		<div class="row"><span class="l">Checkpoint window</span><span class="v">{model.contextLength.toLocaleString()} tokens</span></div>
	{/if}
	{#if model.sizeBytes}
		<div class="row"><span class="l">Size</span><span class="v">{(model.sizeBytes / (1024 ** 3)).toFixed(1)} GiB</span></div>
	{/if}
	{#if model.meta}
		<div class="row"><span class="l">Meta</span><span class="v">{model.meta}</span></div>
	{/if}
	{#if model.lastError}
		<div class="row err"><span class="l">Last error</span><span class="v">{model.lastError}</span></div>
	{/if}

	{#if model.caps.length > 0}
		<Section title="Capabilities">
			{#each model.caps as cap (cap)}
				<div class="cap-row">
					<span class="cap-chip" data-cap={cap}>
						{#if cap === 'vision'}
							<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>
						{:else if cap === 'reasoning'}
							<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M9.5 2A3.5 3.5 0 0 0 6 5.5v.7A2.5 2.5 0 0 0 4 8.5v.6a2.5 2.5 0 0 0 .5 1.5A2.5 2.5 0 0 0 4 12v.6a2.5 2.5 0 0 0 1.2 2A2.5 2.5 0 0 0 5 16.4 2.6 2.6 0 0 0 7.5 19h.6A3.5 3.5 0 0 0 11 22a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1h-1.5zM14 2a1 1 0 0 0-1 1v18a1 1 0 0 0 1 1 3.5 3.5 0 0 0 2.9-3h.6a2.6 2.6 0 0 0 2.5-2.6 2.5 2.5 0 0 0-.2-1.8A2.5 2.5 0 0 0 21 12.6V12a2.5 2.5 0 0 0-.5-1.4A2.5 2.5 0 0 0 21 9.1v-.6A2.5 2.5 0 0 0 19 6.2v-.7A3.5 3.5 0 0 0 15.5 2H14z"/></svg>
						{:else if cap === 'tools'}
							<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 7a4 4 0 0 0 5 5l-3-3 1-1 3 3a4 4 0 0 0-5-5L4 22l-2-2L13 9l1-2z"/></svg>
						{/if}
					</span>
					<span class="cap-text">{capLabels[cap] ?? cap}</span>
				</div>
			{/each}
		</Section>
	{/if}

	{#if showsServerState}
		<Section title="Server-side state">
			<div class="srv-row">
				<span class="srv-label" title="Keep this model loaded persistently. Without pinning, oMLX evicts idle models to free memory.">Pinned</span>
				<button
					type="button"
					class="tg sm"
					class:on={!!runtime?.pinned}
					disabled={pinSaving || runtimeLoading || !runtime}
					onclick={togglePinned}
					aria-label="Toggle pinned"
					aria-pressed={!!runtime?.pinned}
					title="Keep this model loaded; oMLX won't evict it on idle"
				></button>
			</div>
			<p class="srv-hint">
				<strong>Pinned</strong> = the model stays loaded in memory until you unload
				it or restart the daemon. Use it for models you switch to often so
				you don't pay reload latency. Unpinned models are evicted by oMLX
				when idle to free GPU/RAM.
			</p>
			{#if pinError}<p class="srv-err">{pinError}</p>{/if}
		</Section>
	{/if}

	{#if labelEditable && model.dbId}
		<ModelLabelEditor
			dbId={model.dbId}
			label={model.alias}
			discoveredName={model.discoveredName ?? model.name}
			onSaved={onChanged}
		/>
	{/if}

	{#if showsCheckpointing && model.dbId}
		<CheckpointWindowEditor dbId={model.dbId} providerWindow={model.providerContextLength} onSaved={onChanged} />
	{/if}

	{#if providerName === 'omlx'}
		<p class="hint">
			{#if omlxAdminUrl}
				<a href={omlxAdminUrl} target="_blank" rel="noreferrer">Sampling is set in oMLX</a>
			{:else}
				Sampling is set in oMLX.
			{/if}
		</p>
	{/if}

	<!-- An External endpoint lists no input types, so image support is detected per model. -->
	{#if external && serverId}
		<ImageInputEditor {serverId} modelId={model.id} imageInput={model.imageInput} onSaved={onChanged} />
	{/if}

	{#if serverId && model.state === 'loaded'}
		<Section title="Cache">
			{#if cacheLoading}
				<p class="empty">Loading cache stats…</p>
			{:else if cacheSummary && (cacheSummary.count !== undefined || cacheSummary.total_tokens !== undefined)}
				{#if cacheSummary.count !== undefined && cacheSummary.max !== undefined}
					<div class="row"><span class="l">Slots</span><span class="v">{cacheSummary.count} / {cacheSummary.max}</span></div>
				{/if}
				{#if cacheSummary.total_tokens !== undefined}
					<div class="row"><span class="l">Tokens cached</span><span class="v">{cacheSummary.total_tokens.toLocaleString()}</span></div>
				{/if}
				{#if cacheSummary.active_slots !== undefined}
					<div class="row"><span class="l">Active slots</span><span class="v">{cacheSummary.active_slots}</span></div>
				{/if}
			{:else}
				<p class="empty">No active cache entries for this model.</p>
			{/if}
		</Section>
	{/if}
</Modal>

<style>
	.head {
		display: flex; align-items: center; gap: 12px;
		padding: 18px 22px;
		border-bottom: 1px solid var(--border-subtle);
	}
	.title {
		flex: 1;
		display: flex; align-items: center; gap: 10px;
		min-width: 0;
	}
	.model-name {
		font-size: var(--text-body); font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--text-primary);
		overflow-wrap: anywhere;
	}

	.row {
		display: flex; justify-content: space-between; align-items: flex-start;
		gap: 14px;
		padding: 8px 0;
		font-size: var(--text-subhead);
		border-bottom: 1px solid var(--border-subtle);
	}
	.row:last-child { border-bottom: 0; }
	.row .l { color: var(--text-tertiary); flex-shrink: 0; }
	.row .v {
		color: var(--text-primary);
		font-weight: 500;
		text-align: right;
		overflow-wrap: anywhere;
	}
	.row.err .v { color: var(--red); }
	.row code { font-family: 'SF Mono', monospace; font-size: var(--text-footnote); color: var(--text-secondary); }

	.cap-row {
		display: flex; align-items: center; gap: 10px;
		padding: 6px 0;
	}
	.cap-chip {
		display: inline-flex; align-items: center; justify-content: center;
		width: 24px; height: 24px;
		border-radius: var(--radius-xs);
	}
	.cap-chip[data-cap="vision"]    { background: var(--sky-bg);    color: var(--sky); }
	.cap-chip[data-cap="reasoning"] { background: var(--violet-bg); color: var(--violet-text, var(--violet)); }
	.cap-chip[data-cap="tools"]     { background: var(--teal-bg);   color: var(--teal); }
	.cap-text { font-size: var(--text-subhead); color: var(--text-primary); }

	.empty { font-size: var(--text-footnote); color: var(--text-tertiary); padding: 4px 0; margin: 0; }
	.hint { font-size: var(--text-footnote); color: var(--text-tertiary); margin: 12px 0 0; }
	.hint a { color: var(--violet-text); }

	.srv-row {
		display: flex; align-items: center; justify-content: space-between;
		padding: 6px 0;
		font-size: var(--text-subhead);
	}
	.srv-label { color: var(--text-primary); font-weight: 500; }
	.srv-hint {
		font-size: var(--text-footnote); line-height: 1.5;
		color: var(--text-tertiary);
		margin: 4px 0 0 0;
	}
	.srv-err {
		font-size: var(--text-footnote);
		color: var(--red);
		margin: 4px 0 0 0;
	}

	.tg {
		--w: 30px; --h: 17px;
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
	.tg:disabled { opacity: 0.5; cursor: not-allowed; }
	.tg::after {
		content: '';
		position: absolute;
		width: 12px; height: 12px;
		border-radius: var(--radius-circle);
		background: linear-gradient(180deg, #d8d8e0, #b8b8c0);
		top: 1.5px; left: 1.5px;
		transition: all var(--dur-tight) var(--ease-snap);
		box-shadow: 0 1px 2px rgba(0,0,0,0.4);
	}
	.tg.on {
		background: linear-gradient(180deg, var(--violet-2), var(--violet));
		border-color: var(--violet);
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.2), 0 0 8px color-mix(in srgb, var(--violet) 30%, transparent);
	}
	.tg.on::after {
		left: calc(var(--w) - 14.5px);
		background: linear-gradient(180deg, #ffffff, #f0f0f5);
	}

	@media (max-width: 480px) {
		.head { padding: 14px 16px; }
	}
</style>
