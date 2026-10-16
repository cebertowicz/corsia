<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { on } from '$lib/ws/events';
	import { Button } from '$lib/components/ui';
	import { reconcileArtifactsApply, fetchArtifactPane } from '$lib/api/docs';
	import type { ArtifactNode, PaneData } from '$lib/api/artifactPane';
	import { removeArtifactWithUndo } from '$lib/artifactPaneRemoval';
	import { togglePin } from '$lib/artifactPanePin';
	import { toasts } from '$lib/stores/toast';
	import { openBrowserPane } from '$lib/stores/artifactWorkspace';
	import ArtifactRow from './ArtifactRow.svelte';
	import ArtifactWorktreeGroup from './ArtifactWorktreeGroup.svelte';
	import AttentionCard from './AttentionCard.svelte';
	import { fetchRaised } from '$lib/api/attention';
	import type { AttentionItem } from '$lib/api/attention';
	import { failThreadOpen, markArtifactsLoaded, registerArtifactsPane } from '$lib/openTimingRuntime';

	let { threadId = null, conductorThreadId = null, demo = null, onBrowseDocuments }: {
		threadId?: string | null;
		conductorThreadId?: string | null;
		demo?: PaneData | null;
		onBrowseDocuments?: (threadId: string) => void;
	} = $props();

	// True when this pane is rendering the conductor's own thread.
	const conductorMode = $derived(conductorThreadId != null && threadId === conductorThreadId);

	let data = $state<PaneData | null>(null);
	let loaded = $state(false);
	let fetchError = $state(false);
	let raisedItems = $state<AttentionItem[]>([]);
	let raisedError = $state(false);
	let expandedGroups = $state<Record<string, boolean>>({});
	let pendingDisplay = $state<Record<string, boolean>>({});
	let retrying = $state(false);
	let retryingLoad = $state(false);
	let paneGeneration = 0;
	let raisedGeneration = 0;

	// Disposition requests raised to the conductor — a distinct queue from the
	// artifact pane's own needs_attention repair-failure cluster (never merged).
	const primaryNeeds = $derived<AttentionItem[]>(conductorMode ? raisedItems : []);

	// Frames join the pane's one read: while it runs, a frame only marks one more read due. Every
	// other trigger reads at once and becomes the slot's read, so none waits behind a slow or hung
	// one. paneGeneration alone decides whether a response is shown; the slot decides whether to send.
	const slot = { threadId: null as string | null, running: null as Promise<void> | null, again: false };

	function load(): Promise<void> {
		paneGeneration++;
		slot.threadId = threadId;
		slot.again = false;
		const read: Promise<void> = (async () => {
			try {
				await loadPane();
			} finally {
				if (slot.running === read && slot.threadId === threadId) {
					slot.running = null;
					if (slot.again) void load();
				}
			}
		})();
		slot.running = read;
		return read;
	}

	function loadForFrame() {
		if (slot.running && slot.threadId === threadId) slot.again = true;
		else void load();
	}

	async function loadPane() {
		const requestGeneration = paneGeneration;
		if (demo) return;
		const sid = threadId;
		const tok = get(token);
		if (!sid || !tok) {
			data = null;
			loaded = true;
			return;
		}
		try {
			const next = await fetchArtifactPane(sid);
			if (requestGeneration !== paneGeneration) return;
			data = next;
			fetchError = false;
			// The thread's artifact list is resolved (possibly empty) — one half of "usable".
			markArtifactsLoaded(sid);
		} catch {
			if (requestGeneration !== paneGeneration) return;
			// Do NOT clear `data` - a failed refresh must never silently
			// downgrade a populated pane into the "nothing here yet" empty state.
			fetchError = true;
			failThreadOpen(sid);
		}
		if (conductorMode && conductorThreadId) {
			const raisedRequest = ++raisedGeneration;
			try {
				const res = await fetchRaised(tok, conductorThreadId);
				if (requestGeneration !== paneGeneration || raisedRequest !== raisedGeneration) return;
				raisedItems = res.items.filter((it) => it.attention_state === 'raised');
				raisedError = false;
			} catch {
				if (requestGeneration !== paneGeneration || raisedRequest !== raisedGeneration) return;
				// Do NOT reset raisedItems — preserve last known state so the conductor
				// doesn't see an empty queue when the fetch transiently fails.
				raisedError = true;
			}
		}
		if (requestGeneration === paneGeneration) loaded = true;
	}

	async function refreshRaised() {
		const requestGeneration = ++raisedGeneration;
		const tok = get(token);
		if (!tok || !conductorMode || !conductorThreadId) return;
		try {
			const res = await fetchRaised(tok, conductorThreadId);
			if (requestGeneration !== raisedGeneration) return;
			raisedItems = res.items.filter((it) => it.attention_state === 'raised');
			raisedError = false;
		} catch {
			if (requestGeneration !== raisedGeneration) return;
			// Preserve last known raisedItems — a transient failure must not blank the queue.
			raisedError = true;
		}
	}

	$effect(() => {
		if (demo) {
			data = demo;
			loaded = true;
			return;
		}
		threadId;
		conductorMode;
		load();
	});

	// Whether a pane is on screen is what decides if a thread open waits for its
	// artifact list at all (#2280), so this pane reports its own life to the
	// timing runtime — and stops doing so when it moves to another thread.
	$effect(() => {
		if (demo || !threadId) return;
		return registerArtifactsPane(threadId);
	});

	onMount(() => {
		if (demo) return;
		const mine = (d: any) => !threadId || d?.thread_id === threadId;
		const artifactMine = (d: any) => !threadId || d?.affected_thread_ids?.includes(threadId);
		// A comment change can set or clear a row's reply marker; it reaches the pane only as this frame.
		const shown = (d: any) => !!data?.nodes.some((n) =>
			n.kind === 'artifact' ? n.id === d?.artifact_id : n.children.some((c) => c.id === d?.artifact_id));
		const offs = [
			on('connected', () => load()),
			on('artifacts_changed', (d) => artifactMine(d) && loadForFrame()),
			on('doc_comment_changed', (d) => shown(d) && load()),
			on('turn_complete', (d) => mine(d) && load()),
			on('attention_changed', () => conductorMode && refreshRaised()),
		];
		return () => offs.forEach((off) => off && off());
	});

	const hasContent = $derived(
		!!data && (data.nodes.length > 0 || data.needs_attention.length > 0)
	);
	const isEmpty = $derived(
		!!data &&
		!primaryNeeds.length &&
		!(conductorMode && raisedError) &&
		!hasContent
	);
	// A failed refresh with nothing already on screen is its own state: never
	// render "no artifacts yet" for what is actually an unanswered request.
	const showFetchError = $derived(fetchError && !hasContent && !primaryNeeds.length);

	// Cross-reference a needs_attention artifact_id against currently-fetched
	// nodes for a display name — the DTO itself carries id + reason only.
	const artifactNames = $derived.by(() => {
		const map = new Map<string, string>();
		if (!data) return map;
		for (const n of data.nodes) {
			if (n.kind === 'artifact') map.set(n.id, n.name);
			else for (const c of n.children) map.set(c.id, c.name);
		}
		// Settled and quiet registrations are intentionally absent from this map.
		return map;
	});
	function attentionName(artifactId: string): string {
		return artifactNames.get(artifactId) ?? 'Artifact';
	}

	function isGroupExpanded(key: string): boolean {
		return expandedGroups[key] ?? false;
	}
	function toggleGroup(key: string) {
		expandedGroups[key] = !isGroupExpanded(key);
	}

	// Rows and groups can be hidden from this live-work projection.
	function hideRow(artifactId: string) {
		void removeRow(artifactId);
	}

	async function removeRow(artifactId: string) {
		if (!threadId || pendingDisplay[artifactId]) return;
		pendingDisplay[artifactId] = true;
		try {
			await removeArtifactWithUndo(artifactId, threadId, load);
		} finally {
			delete pendingDisplay[artifactId];
		}
	}

	// Pinning keeps a row after its originating plan task settles, and floats it
	// to the top. Shares the pending map with removal so a row takes one
	// in-flight mutation at a time.
	function pinRow(artifactId: string, pinned: boolean) {
		void setRowPinned(artifactId, pinned);
	}

	async function setRowPinned(artifactId: string, pinned: boolean) {
		if (!threadId || pendingDisplay[artifactId]) return;
		pendingDisplay[artifactId] = true;
		try {
			await togglePin(artifactId, threadId, pinned, load);
		} finally {
			delete pendingDisplay[artifactId];
		}
	}

	async function retryReconcile() {
		if (!threadId || retrying) return;
		retrying = true;
		try {
			await reconcileArtifactsApply(threadId);
			await load();
			revealRemainingFailures();
		} catch (e) {
			toasts.show('Retry failed — try again', 'error');
		} finally {
			retrying = false;
		}
	}

	// After the user explicitly retries, open any group still holding a failed
	// child so the aggregate dot leads somewhere. Only on that explicit action —
	// groups stay collapsed on an ordinary refresh.
	function revealRemainingFailures() {
		if (!data) return;
		for (const n of data.nodes) {
			if (n.kind === 'worktree-group' && n.attention) expandedGroups[n.key] = true;
		}
	}

	async function retryLoad() {
		if (retryingLoad) return;
		retryingLoad = true;
		try {
			await load();
		} finally {
			retryingLoad = false;
		}
	}
</script>

{#if !loaded}
	<div class="muted">Loading…</div>
{:else if showFetchError}
	<div class="pane-error" role="status">
		<span>Couldn't load this thread's files.</span>
		<Button variant="secondary" size="sm" loading={retryingLoad} disabled={retryingLoad} onclick={retryLoad}>
			Retry
		</Button>
	</div>
{:else if !data || isEmpty}
	<div class="muted">Files you create, upload, or open in this thread show up here.</div>
{:else}
	{#if fetchError}
		<div class="stale-notice" role="status">
			Couldn't refresh — showing the last loaded files.
			<button type="button" class="inline-retry" disabled={retryingLoad} onclick={retryLoad}>Retry</button>
		</div>
	{/if}

	{#if primaryNeeds.length || (conductorMode && raisedError)}
		<div class="lane-cap needs">Needs you</div>
		{#if conductorMode && raisedError}
			<div class="raised-error">Disposition requests unavailable — refresh to retry</div>
		{/if}
		{#each primaryNeeds as it (it.external_id)}
			<AttentionCard item={it} mode="parent" onDone={() => refreshRaised()} />
		{/each}
	{/if}

	{#if data.needs_attention.length}
		<div class="lane-cap needs-attention">Needs attention</div>
		{#each data.needs_attention as item (item.artifact_id)}
			<div class="attn-row">
				<span class="name" title={attentionName(item.artifact_id)}>{attentionName(item.artifact_id)}</span>
				<span class="attn-reason">{item.reason}</span>
			</div>
		{/each}
		{#if data.needs_attention.some((i) => i.retryable)}
			<div class="retry-wrap">
				<Button variant="secondary" size="sm" loading={retrying} disabled={retrying} onclick={retryReconcile}>
					Retry
				</Button>
			</div>
		{/if}
	{/if}

	{#each data.nodes as n (n.kind === 'artifact' ? n.id : n.key)}
		{#if n.kind === 'artifact'}
			<ArtifactRow item={n} {threadId} pending={!!pendingDisplay[n.id]} onHide={hideRow} onPin={pinRow} />
		{:else}
			<ArtifactWorktreeGroup
				group={n}
				expanded={isGroupExpanded(n.key)}
				onToggle={toggleGroup}
				{threadId}
				{pendingDisplay}
				onHide={hideRow}
				onPin={pinRow}
			/>
		{/if}
	{/each}

{/if}

{#if threadId}
<div class="pane-links">
{#if onBrowseDocuments}
	<button class="browse" type="button" onclick={() => onBrowseDocuments(threadId)}>
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></svg>
		Browse documents…
	</button>
{/if}
	<button class="browse" type="button" onclick={() => openBrowserPane(threadId)}>
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></svg>
		Open browser
	</button>
</div>
{/if}

<style>
	.muted { padding: 10px 14px; font-size: var(--text-subhead, 13px); color: var(--text-tertiary); line-height: 1.5; }

	.lane-cap {
		padding: 8px 8px 3px; font-size: var(--text-footnote); font-weight: 600;
		color: var(--text-tertiary);
	}
	.lane-cap.needs, .lane-cap.needs-attention { color: var(--amber-text); }
	.raised-error {
		padding: 6px 10px; margin: 2px 0; border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--amber) 10%, transparent);
		font-size: var(--text-footnote); color: var(--amber);
	}

	/* A failed fetch, distinct from a genuinely empty pane. */
	.pane-error {
		display: flex; flex-direction: column; align-items: flex-start; gap: 8px;
		padding: 10px 14px; font-size: var(--text-subhead); line-height: 1.5;
		color: var(--text-secondary);
	}
	.stale-notice {
		display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
		padding: 6px 10px; margin: 2px 0; border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--amber) 10%, transparent);
		font-size: var(--text-footnote); color: var(--amber);
	}
	.inline-retry {
		background: none; border: 0; padding: 0; font: inherit; color: inherit;
		text-decoration: underline; cursor: pointer;
	}
	.inline-retry:disabled { opacity: 0.6; cursor: default; }

	.name { font-weight: 500; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }

	.attn-row {
		display: flex; align-items: baseline; gap: 8px; padding: 5px 8px;
		font-size: var(--text-subhead);
	}
	.attn-row .name { flex-shrink: 0; max-width: 45%; }
	.attn-reason { color: var(--text-tertiary); font-size: var(--text-footnote); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.retry-wrap { margin: 2px 8px 6px; }
	.pane-links { margin-top: var(--space-3); }
	.browse {
		display: flex; align-items: center; gap: 8px; width: 100%;
		padding: 8px; border: none;
		background: none; cursor: pointer; font: inherit;
		font-size: var(--text-subhead); color: var(--text-tertiary); text-align: left;
	}
	.browse:hover { color: var(--text-secondary); }
	.browse svg { width: 15px; height: 15px; flex-shrink: 0; }
</style>
