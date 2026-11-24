<script lang="ts">
	import { onDestroy, tick, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { isMobile } from '$lib/stores/viewport';
	import { createActivityQueue } from '$lib/stores/activityQueue.svelte';
	import { activityPhoneItem } from '$lib/stores/activityPhoneItem';
	import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
	import { activityWorkspaceKey } from '$lib/stores/activityWorkspace';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';
	import ActivityList from './ActivityList.svelte';
	import ActivityItemPane from './ActivityItemPane.svelte';
	import { followerOf, nextKey } from './activityItems';

	/**
	 * Activity: everything waiting on the person, ranked by the scope's conductor (CORSIA in the workspace), with the
	 * open item beside the list in the pane the chat uses. The top item opens on arrival; acting re-reads the queue
	 * and the item that followed opens; Next moves on without acting. On a phone the list and the item take turns,
	 * and the app's header carries back to the list and Next.
	 */
	let {
		projectId = null,
		onSelectThread,
	}: {
		projectId?: string | null;
		onSelectThread: (threadId: string) => void | Promise<void>;
	} = $props();

	const store = createActivityQueue(() => projectId);
	const workspaceKey = $derived(activityWorkspaceKey(projectId));

	/** The item open in the pane; on a phone, the item marked in the list until it is tapped open. */
	let openKey = $state<string | null>(null);
	/** Phone only: the item is on screen instead of the list. */
	let phoneOpen = $state(false);
	/** The order the person last saw, so an item that leaves is followed by what came after it. */
	let seenKeys: string[] = [];
	let arrived = false;
	let list = $state<ActivityList | undefined>(undefined);

	$effect(() => {
		void projectId;
		return untrack(() => {
			openKey = null;
			phoneOpen = false;
			seenKeys = [];
			arrived = false;
			return store.start();
		});
	});

	const items = $derived(store.queue?.items ?? []);
	const loading = $derived(store.status === 'loading');
	const openIndex = $derived(items.findIndex((item) => item.key === openKey));
	const openItem = $derived(openIndex >= 0 ? items[openIndex] : null);
	const nextDisabled = $derived(nextKey(items, openKey) === null);

	// Each read: the first opens the top item; later ones keep the open item open while the list re-sorts around it,
	// and when it has left (acted on, or settled elsewhere) open what followed it.
	$effect(() => {
		const current = items;
		if (store.status !== 'ready') return;
		untrack(() => {
			const keys = current.map((item) => item.key);
			if (!arrived) {
				arrived = true;
				openKey = keys[0] ?? null;
			} else if (openKey && !keys.includes(openKey)) {
				openKey = followerOf(seenKeys, openKey, current);
				if (!openKey) phoneOpen = false;
				void refocus();
			} else if (!openKey && keys.length) {
				openKey = keys[0];
			}
			seenKeys = keys;
		});
	});

	/** After the open item leaves, focus that went with it lands on the row now open, never on the page. */
	async function refocus() {
		await tick();
		const active = typeof document === 'undefined' ? null : document.activeElement;
		if (!active || active === document.body) list?.focusOpen();
	}

	function open(key: string) {
		openKey = key;
		if (get(isMobile)) phoneOpen = true;
	}

	function next() {
		const to = nextKey(items, openKey);
		if (!to) return;
		openKey = to;
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
		<div class="list-scroll">
			<ActivityList
				bind:this={list}
				{items}
				count={store.queue?.count ?? 0}
				ranker={store.queue?.ranker ?? null}
				{openKey}
				{loading}
				failed={store.status === 'error'}
				{nextDisabled}
				onOpen={open}
				onNext={next}
				onOpenRanker={() => { const ranker = store.queue?.ranker; if (ranker) openChat(ranker.thread_id); }}
				onRetry={() => void store.reload()}
			/>
		</div>
	</section>
	<section class="pane-col" aria-label={openItem ? openItem.title : 'Open item'}>
		{#if openItem}
			{#key openItem.key}
				<ActivityItemPane item={openItem} {workspaceKey} onActed={() => store.reload()} onOpenChat={openChat} />
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
	.list-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
	.pane-col { position: relative; flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
	.nothing { margin: auto; color: var(--text-tertiary); font-size: var(--text-subhead); }
	/* A phone shows the list or the item, never both. */
	.phone .list-col { width: 100%; border-right: 0; }
	.phone .pane-col { display: none; }
	.phone.phone-item .list-col { display: none; }
	.phone.phone-item .pane-col { display: flex; }
</style>
