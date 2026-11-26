<script lang="ts">
	import { onDestroy, tick, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { isMobile } from '$lib/stores/viewport';
	import { createActivityQueue } from '$lib/stores/activityQueue.svelte';
	import { activityPhoneItem } from '$lib/stores/activityPhoneItem';
	import { activityOpenRequest } from '$lib/stores/activityOpen';
	import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
	import { activityWorkspaceKey } from '$lib/stores/activityWorkspace';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';
	import { emitProductEvent } from '$lib/product_events';
	import { markDocumentsRead, type ActivityQueueItem } from '$lib/api/activityQueue';
	import ActivityList from './ActivityList.svelte';
	import ActivityItemPane from './ActivityItemPane.svelte';
	import { EMPTY_LINE, documentDetailOf, documentOf, followerOf, keepInPlace, tabOf, type ActivityTab, type DecisionDraft } from './activityItems';

	/**
	 * Activity: two lists beside the pane the chat uses. Your tasks holds everything waiting on the person, ranked by
	 * the scope's conductor (CORSIA in the workspace); Notifications holds documents where agents answered the person's
	 * comments, newest reply first. The top task opens on arrival; acting re-reads the queue and the item that followed
	 * it in the same list opens. Switching tabs changes only the list. On a phone the list and the item take turns, and
	 * the app's header carries back to the list.
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
	/** The tab showing; the open item may belong to the other one. */
	let tab = $state<ActivityTab>('tasks');
	/** Each list's order as the person last saw it, so an item that leaves is followed by what came after it. */
	let seen: Record<ActivityTab, string[]> = { tasks: [], notifications: [] };
	/** Each tab's own scroll, restored when the person comes back to it. */
	const scrolls: Record<ActivityTab, number> = { tasks: 0, notifications: 0 };
	/** Rows whose dismiss failed, with the line they show; Mark all read's own failure line. */
	let dismissFailed = $state<Record<string, string>>({});
	let markAllFailed = $state(false);
	let markingAll = $state(false);
	/** The open item, kept where it was while the person reads why the server let it go; released when they move on. */
	let kept = $state<ActivityQueueItem | null>(null);
	let arrived = false;
	let list = $state<ActivityList | undefined>(undefined);
	let listScroll: HTMLElement;
	let paneInstance = $state(0);
	let lastOpen: ActivityQueueItem | null = null;
	let refusedKeep = false;
	let advancing: string | null = null;
	const decisionDrafts = new Map<string, DecisionDraft>();

	$effect(() => {
		void projectId;
		return untrack(() => {
			openKey = null;
			phoneOpen = false;
			tab = 'tasks';
			seen = { tasks: [], notifications: [] };
			dismissFailed = {};
			markAllFailed = false;
			kept = null;
			lastOpen = null;
			refusedKeep = false;
			advancing = null;
			decisionDrafts.clear();
			paneInstance += 1;
			arrived = false;
			emitProductEvent('activity', 'opened', { scope: projectId ? 'project' : 'workspace' });
			return store.start();
		});
	});

	const keptIn = (list: ActivityTab) => (kept && tabOf(kept) === list ? kept : null);
	const tasks = $derived(keepInPlace(store.queue?.items ?? [], keptIn('tasks'), seen.tasks));
	const notifications = $derived(keepInPlace(store.queue?.notifications.items ?? [], keptIn('notifications'), seen.notifications));
	const loading = $derived(store.status === 'loading');
	const openItem = $derived([...tasks, ...notifications].find((item) => item.key === openKey) ?? null);
	/** The open item has left the server's lists and is kept where it was while the person stays on it. */
	const openGone = $derived(!!kept && kept.key === openKey
		&& ![...(store.queue?.items ?? []), ...(store.queue?.notifications.items ?? [])].some((item) => item.key === openKey));
	/** A document the person still has open after its replies were read: its row reads "All replies read". */
	const allReadKey = $derived(openGone && kept?.kind === 'document' ? kept.key : null);

	$effect(() => { if (openItem) lastOpen = openItem; });

	// Reconcile before the DOM update, so a vanished row never unmounts its pane or draft.
	// Transition                     | openKey              | kept      | paneInstance remounts
	// First read                     | first key or null    | null      | no
	// Present key                    | unchanged            | null      | no
	// Acted departure                | follower or null     | null      | yes
	// Same document and thread rekey | new key              | null      | no
	// Other departure                | unchanged            | lastOpen  | no
	// Rows arrive in empty pane      | first key            | null      | no
	// Explicit navigation            | selected key         | null      | yes, if changed
	// Refused act                    | unchanged            | open item | no
	// Scope change                   | null, then first key | null      | yes
	$effect.pre(() => {
		const queue = store.queue;
		const request = $activityOpenRequest;
		if (store.status !== 'ready' || !queue) return;
		untrack(() => {
			const row = listScroll?.querySelector<HTMLElement>('[data-activity-row][aria-current="true"]');
			const top = row?.getBoundingClientRect().top;
			const instance = paneInstance;
			const lists = { tasks: queue.items, notifications: queue.notifications.items };
			const current = [...queue.items, ...queue.notifications.items];
			const keys = current.map((item) => item.key);
			// Another surface asked for one of the to-dos: it opens in Your tasks, as a tap on its row would.
			const wanted = request?.externalId ? lists.tasks.find((item) => item.external_id === request.externalId) : undefined;
			if (request) activityOpenRequest.set(null);
			if (wanted) {
				arrived = true;
				tab = 'tasks';
				moveTo(wanted.key);
				if ($isMobile) phoneOpen = true;
			}
			if (!arrived) {
				arrived = true;
				openKey = lists.tasks[0]?.key ?? null;
			} else if (openKey && !keys.includes(openKey) && advancing === openKey && lastOpen) {
				advance(lists[tabOf(lastOpen)], openKey);
			} else if (openKey && !keys.includes(openKey) && lastOpen) {
				const doc = documentOf(lastOpen);
				const matches = doc ? current.filter((item) => {
					const candidate = documentOf(item);
					return item.kind === lastOpen?.kind && !!candidate
						&& (doc.artifactId ? candidate.artifactId === doc.artifactId : !!doc.path && candidate.path === doc.path)
						&& candidate.commentId === doc.commentId && item.source.thread_id === lastOpen?.source.thread_id;
				}) : [];
				if (matches.length === 1) {
					openKey = matches[0].key;
					kept = null;
				} else kept = lastOpen;
			} else if (!openKey && lists[tab].length) openKey = lists[tab][0].key;
			else if (openKey && keys.includes(openKey)) kept = null;
			const keptFor = (list: ActivityTab) => (kept && tabOf(kept) === list ? kept : null);
			seen = {
				tasks: keepInPlace(lists.tasks, keptFor('tasks'), seen.tasks).map((item) => item.key),
				notifications: keepInPlace(lists.notifications, keptFor('notifications'), seen.notifications).map((item) => item.key),
			};
			const listed = new Set(keys);
			if (Object.keys(dismissFailed).some((key) => !listed.has(key))) {
				dismissFailed = Object.fromEntries(Object.entries(dismissFailed).filter(([key]) => listed.has(key)));
			}
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

	/** Opening another row is moving on: a kept item is let go. */
	function moveTo(key: string) {
		if (key !== openKey) {
			advancing = null;
			refusedKeep = false;
			kept = null;
			paneInstance += 1;
		}
		openKey = key;
	}

	/** Keep an approved review's unread document open; otherwise advance within its list. */
	function advance(current: readonly ActivityQueueItem[], key: string) {
		advancing = null;
		kept = null;
		const artifact = lastOpen?.kind === 'review' ? documentOf(lastOpen)?.artifactId
			: lastOpen?.kind === 'draft' && 'unread_replies' in lastOpen.detail && lastOpen.detail.unread_replies
				&& 'artifact_id' in lastOpen.detail ? lastOpen.detail.artifact_id : undefined;
		const document = artifact ? store.queue?.notifications.items.find((item) => documentDetailOf(item)?.artifact_id === artifact) : undefined;
		if (document) { openKey = document.key; return; }
		const list = lastOpen ? tabOf(lastOpen) : tab;
		const following = followerOf(seen[list], key, current);
		if (following) moveTo(following);
		else { openKey = null; phoneOpen = false; paneInstance += 1; }
		void refocus();
	}

	async function acted(instance: number) {
		if (instance !== paneInstance) return;
		const key = openKey;
		const keep = refusedKeep;
		refusedKeep = false;
		// A send claim can remove the draft before its review settles. Read the final lists first,
		// so an unread document that returns to Notifications remains open.
		if (!keep && key && lastOpen?.kind === 'draft') {
			await store.reload();
			await tick();
			if (instance !== paneInstance || openKey !== key || store.failure !== null) return;
			const current = store.queue?.items ?? [];
			if (!current.some((item) => item.key === key)) advance(current, key);
			return;
		}
		if (!keep && key) {
			advancing = key;
			const queue = store.queue;
			const list = lastOpen ? tabOf(lastOpen) : tab;
			const current = (list === 'tasks' ? queue?.items : queue?.notifications.items) ?? [];
			if (!current.some((item) => item.key === key)) advance(current, key);
		}
		await store.reload();
		await tick();
		if (advancing === key) advancing = null;
	}

	function open(key: string) {
		markAllFailed = false;
		const kind = [...tasks, ...notifications].find((item) => item.key === key)?.kind;
		moveTo(key);
		if (get(isMobile)) phoneOpen = true;
		emitProductEvent('activity', 'row_opened', { kind });
	}

	/** Keep an open item; otherwise select the destination's first row without leaving the phone list. */
	async function switchTab(to: ActivityTab) {
		if (to === tab) return;
		scrolls[tab] = listScroll?.scrollTop ?? 0;
		tab = to;
		const first = (to === 'tasks' ? tasks : notifications)[0];
		if (!openKey && first) moveTo(first.key);
		emitProductEvent('activity', 'tab_opened', { tab: to });
		await tick();
		if (listScroll) listScroll.scrollTop = scrolls[to];
	}

	const through = (item: ActivityQueueItem) => {
		const detail = documentDetailOf(item);
		return detail ? { artifact_id: detail.artifact_id, through_reply_id: detail.latest_reply_id } : null;
	};

	/** × or a swipe: the same call as Done, without opening the document. The open one is acted on as Done is. */
	async function dismiss(key: string) {
		markAllFailed = false;
		const item = notifications.find((row) => row.key === key);
		const read = item && through(item);
		if (!read) return;
		const { [key]: _, ...rest } = dismissFailed;
		dismissFailed = rest;
		try {
			await markDocumentsRead([read]);
		} catch {
			dismissFailed = { ...dismissFailed, [key]: 'Couldn’t mark these replies read. Try again.' };
			return;
		}
		emitProductEvent('activity', 'dismissed');
		if (key === openKey) await acted(paneInstance);
		else await store.reload();
	}

	/** Every listed document in one call; the open one stays open, reading "All replies read." */
	async function markAllRead() {
		if (markingAll) return;
		const documents = notifications.map(through).filter((read) => read !== null);
		if (!documents.length) return;
		markingAll = true;
		markAllFailed = false;
		try {
			await markDocumentsRead(documents);
			emitProductEvent('activity', 'marked_all_read', { count: documents.length });
			await store.reload();
		} catch {
			markAllFailed = true;
		} finally {
			markingAll = false;
		}
	}

	function openChat(threadId: string) {
		void onSelectThread(threadId);
	}

	// A phone shows one screen at a time; the header gives the open item its way back to the list it came from.
	$effect(() => {
		const showing = $isMobile && phoneOpen && openItem !== null;
		activityPhoneItem.set(showing
			? { backLabel: tab === 'notifications' ? 'Notifications' : 'Your tasks', onBack: () => { phoneOpen = false; void tick().then(() => { if (listScroll) listScroll.scrollTop = scrolls[tab]; }); } }
			: null);
	});

	/** On a phone the list scrolls away while an item is open, so its place is saved as the item opens. */
	$effect.pre(() => {
		if ($isMobile && phoneOpen) untrack(() => { scrolls[tab] = listScroll?.scrollTop ?? scrolls[tab]; });
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
				{tab}
				{tasks}
				{notifications}
				taskCount={store.queue?.count ?? 0}
				notificationCount={store.queue?.notifications.count ?? 0}
				ranker={store.queue?.ranker ?? null}
				{openKey}
				{allReadKey}
				{dismissFailed}
				{markingAll}
				markAllFailed={markAllFailed ? 'Couldn’t mark these read. Try again.' : null}
				{loading}
				failure={store.status === 'error' ? store.failure : null}
				stale={store.status === 'ready' && store.failure !== null}
				onTab={(to) => void switchTab(to)}
				onOpen={open}
				onDismiss={(key) => void dismiss(key)}
				onMarkAllRead={() => void markAllRead()}
				onOpenRanker={() => { const ranker = store.queue?.ranker; if (ranker) openChat(ranker.thread_id); }}
				onRetry={() => void store.reload()}
			/>
		</div>
		<p class="nothere">Not gathered here: email you receive, and questions and replies inside chats. Open the chat to see them.</p>
	</section>
	<section class="pane-col" bind:offsetWidth={paneWidth} aria-label={openItem ? openItem.title : 'Nothing open'}>
		{#if openItem}
			{#key paneInstance}
				{@const mountedInstance = paneInstance}
				{@const mountedKey = openItem.key}
				<ActivityItemPane item={openItem} {workspaceKey} draft={decisionDrafts.get(mountedKey)} onDraft={(draft) => decisionDrafts.set(mountedKey, draft)} onClearDraft={() => decisionDrafts.delete(mountedKey)} allRead={allReadKey !== null} stale={$isMobile && phoneOpen && store.failure !== null} noLonger={openGone && kept?.kind !== 'document'} onActed={() => acted(mountedInstance)} onKeep={() => { if (mountedInstance === paneInstance) { kept = openItem; refusedKeep = true; } }} onOpenChat={openChat} />
			{/key}
		{:else if !loading && store.status === 'ready'}
			<div class="nothing">{EMPTY_LINE[tab]}</div>
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
	/* One footer position on every tab: under the list, outside its scroll. */
	.nothere {
		flex: none; margin: 0; padding: var(--space-3) var(--space-5) var(--space-4);
		border-top: 1px solid var(--border-subtle); color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.4;
	}
	/* Pane notices render below the pane's own header. */
	.pane-col { position: relative; flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
	.nothing { margin: auto; color: var(--text-tertiary); font-size: var(--text-subhead); }
	/* A phone shows the list or the item, never both. */
	.phone .list-col { width: 100%; border-right: 0; }
	.phone .pane-col { display: none; }
	.phone.phone-item .list-col { display: none; }
	/* On a phone the app bar carries back, so the item's tab row sits below it rather than in it. */
	.phone.phone-item .pane-col { display: flex; margin-top: var(--header-bottom); }
</style>
