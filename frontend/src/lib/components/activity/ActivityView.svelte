<script lang="ts">
	import { onDestroy, tick, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { isMobile } from '$lib/stores/viewport';
	import { createActivityQueue } from '$lib/stores/activityQueue.svelte';
	import { activityPhoneItem } from '$lib/stores/activityPhoneItem';
	import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
	import { activityWorkspaceKey } from '$lib/stores/activityWorkspace';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';
	import { emitProductEvent } from '$lib/product_events';
	import type { ActivityQueueItem } from '$lib/api/activityQueue';
	import ActivityList from './ActivityList.svelte';
	import ActivityItemPane from './ActivityItemPane.svelte';
	import { documentOf, followerOf, keepInPlace, nextKey } from './activityItems';

	/**
	 * Activity: everything waiting on the person, ranked by the scope's conductor (CORSIA in the workspace), with the
	 * open item beside the list in the pane the chat uses. The top item opens on arrival; acting re-reads the queue
	 * and the item that followed opens; Next moves on without acting. On a phone the list and the item take turns,
	 * and the app's header carries back to the list and Next.
	 */
	let {
		projectId = null,
		onSelectThread,
		onHeaderInset = () => {},
	}: {
		projectId?: string | null;
		onSelectThread: (threadId: string) => void | Promise<void>;
		/** The item pane's width, published as the chat's document column is, so the app header gives that segment to the pane's tab and tools. */
		onHeaderInset?: (width: number | null) => void;
	} = $props();

	let paneWidth = $state(0);
	$effect(() => {
		onHeaderInset(!$isMobile && paneWidth > 0 ? paneWidth : null);
		return () => onHeaderInset(null);
	});

	const store = createActivityQueue(() => projectId);
	const workspaceKey = $derived(activityWorkspaceKey(projectId));

	/** The item open in the pane; on a phone, the item marked in the list until it is tapped open. */
	let openKey = $state<string | null>(null);
	/** Phone only: the item is on screen instead of the list. */
	let phoneOpen = $state(false);
	/** The order the person last saw, so an item that leaves is followed by what came after it. */
	let seenKeys: string[] = [];
	/** The open item, kept where it was while the person reads why the server let it go; released when they move on. */
	let kept = $state<ActivityQueueItem | null>(null);
	let arrived = false;
	let list = $state<ActivityList | undefined>(undefined);
	let listScroll: HTMLElement;
	let paneInstance = $state(0);
	let lastOpen: ActivityQueueItem | null = null;
	let refusedKeep = false;
	let advancing: string | null = null;

	$effect(() => {
		void projectId;
		return untrack(() => {
			openKey = null;
			phoneOpen = false;
			seenKeys = [];
			kept = null;
			lastOpen = null;
			refusedKeep = false;
			advancing = null;
			paneInstance += 1;
			arrived = false;
			emitProductEvent('activity', 'opened', { scope: projectId ? 'project' : 'workspace' });
			return store.start();
		});
	});

	const items = $derived(keepInPlace(store.queue?.items ?? [], kept, seenKeys));
	const loading = $derived(store.status === 'loading');
	const openIndex = $derived(items.findIndex((item) => item.key === openKey));
	const openItem = $derived(openIndex >= 0 ? items[openIndex] : null);
	const nextDisabled = $derived(nextKey(items, openKey) === null);

	$effect(() => { if (openItem) lastOpen = openItem; });

	// Reconcile before the DOM update, so a vanished row never unmounts its pane or draft.
	$effect.pre(() => {
		const current = store.queue?.items;
		if (store.status !== 'ready' || !current) return;
		untrack(() => {
			const row = listScroll?.querySelector<HTMLElement>('[data-activity-row][aria-current="true"]');
			const top = row?.getBoundingClientRect().top;
			const instance = paneInstance;
			const keys = current.map((item) => item.key);
			if (!arrived) {
				arrived = true;
				openKey = keys[0] ?? null;
			} else if (openKey && !keys.includes(openKey) && advancing === openKey) {
				advance(current, openKey);
			} else if (openKey && !keys.includes(openKey) && lastOpen) {
				const doc = documentOf(lastOpen);
				const matches = doc ? current.filter((item) => {
					const candidate = documentOf(item);
					return item.kind === lastOpen?.kind && candidate?.artifactId === doc.artifactId
						&& candidate.commentId === doc.commentId;
				}) : [];
				if (matches.length === 1) {
					openKey = matches[0].key;
					kept = null;
				} else kept = lastOpen;
			} else if (!openKey && keys.length) openKey = keys[0];
			else if (openKey && keys.includes(openKey)) kept = null;
			seenKeys = keepInPlace(current, kept, seenKeys).map((item) => item.key);
			if (row && top !== undefined && row.getClientRects().length) void tick().then(() => {
				if (paneInstance !== instance) return;
				const updated = listScroll?.querySelector<HTMLElement>('[data-activity-row][aria-current="true"]');
				if (updated) listScroll.scrollTop += updated.getBoundingClientRect().top - top;
			});
		});
	});

	/** After the open item leaves, focus that went with it lands on the row now open, never on the page. */
	async function refocus() {
		await tick();
		const active = typeof document === 'undefined' ? null : document.activeElement;
		if (!active || active === document.body) list?.focusOpen();
	}

	/** Opening another row, or Next, is moving on: a kept item is let go. */
	function moveTo(key: string) {
		if (key !== openKey) {
			advancing = null;
			refusedKeep = false;
			kept = null;
			paneInstance += 1;
		}
		openKey = key;
	}

	function advance(current: readonly ActivityQueueItem[], key: string) {
		advancing = null;
		kept = null;
		const following = followerOf(seenKeys, key, current);
		if (following) moveTo(following);
		else { openKey = null; phoneOpen = false; paneInstance += 1; }
		void refocus();
	}

	async function acted(instance: number) {
		if (instance !== paneInstance) return;
		const key = openKey;
		const keep = refusedKeep;
		refusedKeep = false;
		if (!keep && key) {
			advancing = key;
			const current = store.queue?.items ?? [];
			if (!current.some((item) => item.key === key)) advance(current, key);
		}
		await store.reload();
		await tick();
		if (advancing === key) advancing = null;
	}

	function open(key: string) {
		const kind = items.find((item) => item.key === key)?.kind;
		moveTo(key);
		if (get(isMobile)) phoneOpen = true;
		emitProductEvent('activity', 'row_opened', { kind });
	}

	function next() {
		const to = nextKey(items, openKey);
		if (!to) return;
		moveTo(to);
		emitProductEvent('activity', 'next');
		void tick().then(() => {
			if (!get(isMobile)) list?.focusOpen();
		});
	}

	function openChat(threadId: string) {
		void onSelectThread(threadId);
	}

	// A phone shows one screen at a time; the header gives the open item its way back and Next.
	$effect(() => {
		const item = openItem;
		const showing = $isMobile && phoneOpen && item !== null;
		activityPhoneItem.set(showing
			? {
				position: openIndex + 1,
				count: items.length,
				nextDisabled,
				nextTitle: items[openIndex + 1]?.title ?? null,
				onBack: () => { phoneOpen = false; },
				onNext: next,
			}
			: null);
	});

	onDestroy(() => {
		activityPhoneItem.set(null);
		// Activity's pane closes with it; each document is closed the ordinary way, so a pending edit saves.
		const key = untrack(() => workspaceKey);
		for (const tab of get(threadWorkspaces)[key]?.tabs ?? []) void requestArtifactTabClose(key, tab, () => {});
	});
</script>

<div class="activity" class:phone={$isMobile} class:phone-item={$isMobile && phoneOpen && openItem}>
	<section class="list-col" aria-label="Activity">
		<div class="list-scroll" bind:this={listScroll}>
			<ActivityList
				bind:this={list}
				{items}
				count={store.queue?.count ?? 0}
				ranker={store.queue?.ranker ?? null}
				{openKey}
				{loading}
				failure={store.status === 'error' ? store.failure : null}
				{nextDisabled}
				onOpen={open}
				onNext={next}
				onOpenRanker={() => { const ranker = store.queue?.ranker; if (ranker) openChat(ranker.thread_id); }}
				onRetry={() => void store.reload()}
			/>
		</div>
	</section>
	<section class="pane-col" bind:offsetWidth={paneWidth} aria-label={openItem ? openItem.title : 'Nothing open'}>
		{#if openItem}
			{#key paneInstance}
				{@const mountedInstance = paneInstance}
				<ActivityItemPane item={openItem} {workspaceKey} noLonger={!!kept && kept.kind !== 'reply' && !(store.queue?.items ?? []).some((item) => item.key === openKey)} onActed={() => acted(mountedInstance)} onKeep={() => { if (mountedInstance === paneInstance) { kept = openItem; refusedKeep = true; } }} onOpenChat={openChat} />
			{/key}
		{:else if !loading && store.status === 'ready'}
			<div class="nothing">Nothing to open.</div>
		{/if}
	</section>
</div>

<style>
	.activity { flex: 1; min-height: 0; display: flex; overflow: hidden; }
	.list-col {
		display: flex; flex-direction: column; flex: none; width: clamp(320px, 36%, 455px); min-height: 0;
		border-right: 1px solid var(--border-subtle);
		/* The app's header names Activity in the band above the list. */
		padding-top: var(--header-bottom); box-sizing: border-box;
	}
	.list-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; overflow-anchor: none; }
	/* Pane notices render below the pane's own header. */
	.pane-col { position: relative; flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
	.nothing { margin: auto; color: var(--text-tertiary); font-size: var(--text-subhead); }
	/* A phone shows the list or the item, never both. */
	.phone .list-col { width: 100%; border-right: 0; }
	.phone .pane-col { display: none; }
	.phone.phone-item .list-col { display: none; }
	/* On a phone the app bar carries back and Next, so the item's tab row sits below it rather than in it. */
	.phone.phone-item .pane-col { display: flex; margin-top: var(--header-bottom); }
</style>
