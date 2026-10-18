<script lang="ts">
	import { threads, activeThreadId, isNavAgent, saveThreadTitle } from '$lib/stores/threads';
	import { uiScope } from '$lib/stores/uiScope';
	import { threadActivity } from '$lib/stores/activity';
	import { token } from '$lib/stores/auth';
	import { toasts } from '$lib/stores/toast';
	import { pinnedConversations, togglePin } from '$lib/stores/pinnedConversations';
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import type { Thread } from '$lib/stores/threads';
	import { createSidebarSortMemo } from './agentVisibility';
	import { bucketOf, TIME_BUCKETS } from '$lib/time';
	import { haptic } from '$lib/haptic';
	import SidebarThreadRow from './SidebarThreadRow.svelte';
	import { onMount } from 'svelte';
	import { fly } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';

	let {
		onSelectThread,
		onDeleteThread,
		onArchiveThread,
		onOpenAttention,
		onOpenContextMenu,
	}: {
		onSelectThread: (id: string) => void | Promise<void>;
		onDeleteThread: (id: string) => void;
		onArchiveThread?: (id: string) => void;
		onOpenAttention?: (threadId: string, title: string) => void;
		onOpenContextMenu: (e: MouseEvent, id: string) => void;
	} = $props();

	let renamingThreadId = $state<string | null>(null);
	let renameValue = $state('');

	let threadListEl = $state<HTMLDivElement | undefined>(undefined);

	onMount(() => {
		if (!threadListEl) return;
		const el = threadListEl;
		el.addEventListener('touchmove', onThreadTouchMove, { passive: false });
		return () => el.removeEventListener('touchmove', onThreadTouchMove);
	});

	type RevealedSide = 'left' | 'right' | null;
	let swipingThreadId = $state<string | null>(null);
	let revealedThreadId = $state<string | null>(null);
	let revealedSide = $state<RevealedSide>(null);
	let swipeDelta = $state(0);
	let swipeStartX = 0;
	let swipeStartY = 0;
	let baseDelta = 0;
	let swipeTimer: ReturnType<typeof setTimeout> | null = null;
	let isSwipeGesture = $state(false);
	let didCrossThreshold = false;
	const PIN_ZONE_WIDTH = 80;
	const ARCHIVE_ZONE_WIDTH = 72;
	const DELETE_ZONE_WIDTH = 72;
	const RIGHT_ZONE_WIDTH = ARCHIVE_ZONE_WIDTH + DELETE_ZONE_WIDTH;
	const SWIPE_DEAD_ZONE = 8;
	const ACTIVATION_RATIO = 0.4;
	const RUBBER_BAND = 0.35;

	// Persistent Agents render in the Agent zone (RailScopedSection), never here —
	// `isNavAgent` is the shared predicate that keeps the two lists disjoint.
	let scopedThreads = $derived($threads.filter(s =>
		($uiScope ? s.project_id === $uiScope.id : !s.project_id) && !isNavAgent(s)
	));
	const sortPinnedThreads = createSidebarSortMemo();
	const sortUnpinnedThreads = createSidebarSortMemo();
	let pinnedThreads = $derived(sortPinnedThreads(
		scopedThreads.filter(s => $pinnedConversations.has(s.id)), $threadPlanStates,
	));
	let unpinnedThreads = $derived(sortUnpinnedThreads(
		scopedThreads.filter(s => !$pinnedConversations.has(s.id)), $threadPlanStates,
	));

	let timeGroups = $derived.by(() => {
		const groups: Record<string, Thread[]> = {};
		for (const s of unpinnedThreads) (groups[bucketOf(s.updated_at)] ??= []).push(s);
		return TIME_BUCKETS.filter(b => groups[b]?.length).map(b => ({ label: b, threads: groups[b] }));
	});

	async function handleSelectThread(id: string): Promise<boolean> {
		if (revealedThreadId) {
			revealedThreadId = null;
			return false;
		}
		await onSelectThread(id);
		return true;
	}

	export function startRename(id: string) {
		const thread = $threads.find(s => s.id === id);
		if (thread) {
			renamingThreadId = thread.id;
			renameValue = thread.title;
		}
	}

	async function finishRename() {
		if (!renamingThreadId || !renameValue.trim()) {
			renamingThreadId = null;
			return;
		}
		const id = renamingThreadId;
		const title = renameValue.trim();
		const authToken = $token;
		renamingThreadId = null;
		if (!authToken) {
			toasts.show("Couldn't rename conversation", 'error');
			return;
		}
		try {
			await saveThreadTitle(id, title, authToken);
		} catch {
			toasts.show("Couldn't rename conversation", 'error');
		}
	}

	function snapTo(side: RevealedSide) {
		if (side === 'left') {
			revealedSide = 'left';
			swipeDelta = PIN_ZONE_WIDTH;
		} else if (side === 'right') {
			revealedSide = 'right';
			swipeDelta = -RIGHT_ZONE_WIDTH;
		} else {
			revealedSide = null;
			swipeDelta = 0;
		}
	}

	function onThreadTouchStart(e: TouchEvent, id: string) {
		if (revealedThreadId && revealedThreadId !== id) {
			revealedThreadId = null;
			revealedSide = null;
		}

		swipingThreadId = id;
		swipeStartX = e.touches[0].clientX;
		swipeStartY = e.touches[0].clientY;
		isSwipeGesture = false;
		didCrossThreshold = false;

		if (revealedThreadId === id && revealedSide === 'left') {
			baseDelta = PIN_ZONE_WIDTH;
		} else if (revealedThreadId === id && revealedSide === 'right') {
			baseDelta = -RIGHT_ZONE_WIDTH;
		} else {
			baseDelta = 0;
		}
		swipeDelta = baseDelta;

		swipeTimer = setTimeout(() => {
			if (!isSwipeGesture) {
				const touch = e.touches[0];
				onOpenContextMenu(
					new MouseEvent('contextmenu', { clientX: touch.clientX, clientY: touch.clientY }),
					id
				);
			}
		}, 500);
	}

	function onThreadTouchMove(e: TouchEvent) {
		if (!swipingThreadId) return;
		const dx = e.touches[0].clientX - swipeStartX;
		const dy = e.touches[0].clientY - swipeStartY;

		if (!isSwipeGesture && Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > SWIPE_DEAD_ZONE) {
			swipingThreadId = null;
			swipeDelta = baseDelta;
			if (swipeTimer) { clearTimeout(swipeTimer); swipeTimer = null; }
			return;
		}

		if (Math.abs(dx) > SWIPE_DEAD_ZONE && Math.abs(dx) > Math.abs(dy) * 1.4) {
			isSwipeGesture = true;
			if (swipeTimer) { clearTimeout(swipeTimer); swipeTimer = null; }
		}

		if (!isSwipeGesture) return;

		e.preventDefault();

		let raw = baseDelta + dx;
		if (raw > PIN_ZONE_WIDTH) {
			raw = PIN_ZONE_WIDTH + (raw - PIN_ZONE_WIDTH) * RUBBER_BAND;
		} else if (raw < -RIGHT_ZONE_WIDTH) {
			raw = -RIGHT_ZONE_WIDTH + (raw + RIGHT_ZONE_WIDTH) * RUBBER_BAND;
		}
		swipeDelta = raw;

		const willActivate =
			swipeDelta > PIN_ZONE_WIDTH * ACTIVATION_RATIO ||
			swipeDelta < -RIGHT_ZONE_WIDTH * ACTIVATION_RATIO;
		if (willActivate && !didCrossThreshold) {
			didCrossThreshold = true;
			haptic();
		} else if (!willActivate && didCrossThreshold) {
			didCrossThreshold = false;
		}
	}

	function onThreadTouchEnd() {
		if (swipeTimer) { clearTimeout(swipeTimer); swipeTimer = null; }
		if (!swipingThreadId) return;

		const id = swipingThreadId;
		swipingThreadId = null;

		if (!isSwipeGesture) {
			snapTo(revealedThreadId === id ? revealedSide : null);
			return;
		}

		if (swipeDelta > PIN_ZONE_WIDTH * ACTIVATION_RATIO) {
			revealedThreadId = id;
			snapTo('left');
		} else if (swipeDelta < -RIGHT_ZONE_WIDTH * ACTIVATION_RATIO) {
			revealedThreadId = id;
			snapTo('right');
		} else {
			revealedThreadId = null;
			snapTo(null);
		}
	}

	function onPinTap(id: string, e: MouseEvent | TouchEvent) {
		e.stopPropagation();
		e.preventDefault();
		togglePin(id);
		haptic();
		revealedThreadId = null;
		snapTo(null);
	}

	function onArchiveTap(id: string, e: MouseEvent | TouchEvent) {
		e.stopPropagation();
		e.preventDefault();
		revealedThreadId = null;
		snapTo(null);
		onArchiveThread?.(id);
	}

	function onDeleteTap(id: string, e: MouseEvent | TouchEvent) {
		e.stopPropagation();
		e.preventDefault();
		revealedThreadId = null;
		snapTo(null);
		onDeleteThread(id);
	}

	function cancelRename() {
		renamingThreadId = null;
	}

	function transformForThread(id: string): number {
		const swiping = swipingThreadId === id;
		const revealed = revealedThreadId === id;
		if (swiping) return swipeDelta;
		if (revealed && revealedSide === 'left') return PIN_ZONE_WIDTH;
		if (revealed && revealedSide === 'right') return -RIGHT_ZONE_WIDTH;
		return 0;
	}
</script>

	<div class="thread-list" bind:this={threadListEl}>
	{#key $uiScope?.id ?? 'workspace'}
		<div class="scope-pane" in:fly={{ y: 6, duration: 180, easing: cubicOut }}>
			{#if pinnedThreads.length > 0}
				<div class="list-section-label">Pinned</div>
				{#each pinnedThreads as thread (thread.id)}
					{@const transformX = transformForThread(thread.id)}
					<SidebarThreadRow
						thread={thread}
						isPinned={$pinnedConversations.has(thread.id)}
						isActive={$activeThreadId === thread.id}
						activity={$threadActivity[thread.id]}
						renaming={renamingThreadId === thread.id}
						bind:renameValue
						swiping={swipingThreadId === thread.id}
						{isSwipeGesture}
						transformX={transformX}
						leftZoneX={Math.max(0, transformX) - PIN_ZONE_WIDTH}
						rightZoneX={Math.min(0, transformX) + RIGHT_ZONE_WIDTH}
						pinArmed={swipingThreadId === thread.id && swipeDelta > PIN_ZONE_WIDTH * ACTIVATION_RATIO}
						deleteArmed={swipingThreadId === thread.id && swipeDelta < -RIGHT_ZONE_WIDTH * ACTIVATION_RATIO}
						onTouchStart={onThreadTouchStart}
						onTouchEnd={onThreadTouchEnd}
						onSelect={handleSelectThread}
						{onOpenAttention}
						{onOpenContextMenu}
						onPin={onPinTap}
						onArchive={onArchiveTap}
						onDelete={onDeleteTap}
						onStartRename={startRename}
						onFinishRename={finishRename}
						onCancelRename={cancelRename}
					/>
				{/each}
			{/if}
			{#each timeGroups as group (group.label)}
				<div class="list-section-label">{group.label}</div>
				{#each group.threads as thread (thread.id)}
					{@const transformX = transformForThread(thread.id)}
					<SidebarThreadRow
						thread={thread}
						isPinned={$pinnedConversations.has(thread.id)}
						isActive={$activeThreadId === thread.id}
						activity={$threadActivity[thread.id]}
						renaming={renamingThreadId === thread.id}
						bind:renameValue
						swiping={swipingThreadId === thread.id}
						{isSwipeGesture}
						transformX={transformX}
						leftZoneX={Math.max(0, transformX) - PIN_ZONE_WIDTH}
						rightZoneX={Math.min(0, transformX) + RIGHT_ZONE_WIDTH}
						pinArmed={swipingThreadId === thread.id && swipeDelta > PIN_ZONE_WIDTH * ACTIVATION_RATIO}
						deleteArmed={swipingThreadId === thread.id && swipeDelta < -RIGHT_ZONE_WIDTH * ACTIVATION_RATIO}
						onTouchStart={onThreadTouchStart}
						onTouchEnd={onThreadTouchEnd}
						onSelect={handleSelectThread}
						{onOpenAttention}
						{onOpenContextMenu}
						onPin={onPinTap}
						onArchive={onArchiveTap}
						onDelete={onDeleteTap}
						onStartRename={startRename}
						onFinishRename={finishRename}
						onCancelRename={cancelRename}
					/>
				{/each}
			{/each}
			{#if scopedThreads.length === 0}
				<div class="empty-threads">No conversations here yet</div>
			{/if}
		</div>
	{/key}
</div>

<style>
	.thread-list {
		padding: 8px;
		min-width: var(--sidebar-width);
		padding-bottom: 96px;
	}
	.list-section-label {
		font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary);
		padding: 8px 10px 4px;
	}
	.empty-threads {
		padding: 24px 16px; text-align: center; color: var(--text-tertiary); font-size: var(--text-subhead);
	}
	@media (max-width: 768px) {
		.list-section-label {
			padding: 8px 10px 4px;
		}
		.thread-list {
			padding-bottom: 120px;
		}
	}
</style>
