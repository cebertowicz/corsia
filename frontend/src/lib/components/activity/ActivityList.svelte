<script lang="ts">
	import { tick } from 'svelte';
	import type { ActivityQueueItem, ActivityRanker } from '$lib/api/activityQueue';
	import { formatTime } from '$lib/time';
	import {
		EMPTY_LINE, KIND_LABEL, ageLabel, documentDetailOf, latestLabel, rankedAt, reviewOf, unreadRepliesLabel, type ActivityTab,
	} from './activityItems';

	/**
	 * Activity's two lists under one tab row. Your tasks: one line per item in the ranker's order; the open item shows
	 * its full title and why it sits there. Notifications: one line per document, newest reply first, each with a ×
	 * (or a swipe on a phone) that marks it read, and Mark all read. While the first read is in flight the tabs show
	 * over greyed rows.
	 */
	let {
		tab,
		tasks,
		notifications,
		taskCount,
		notificationCount,
		ranker,
		openKey,
		allReadKey = null,
		dismissFailed = {},
		markingAll = false,
		markAllFailed = null,
		notice = null,
		loading = false,
		failure = null,
		stale = false,
		onTab,
		onOpen,
		onDismiss,
		onMarkAllRead,
		onOpenRanker,
		onRetry,
	}: {
		tab: ActivityTab;
		tasks: readonly ActivityQueueItem[];
		notifications: readonly ActivityQueueItem[];
		taskCount: number;
		notificationCount: number;
		ranker: ActivityRanker | null;
		/** The open item, or on a phone the marked one. */
		openKey: string | null;
		/** The open document whose replies were all read while it stayed open. */
		allReadKey?: string | null;
		/** Rows whose dismiss failed, with the line each shows. */
		dismissFailed?: Record<string, string>;
		markingAll?: boolean;
		markAllFailed?: string | null;
		/** A passing line above the rows: a tapped notification's to-do is gone. */
		notice?: string | null;
		loading?: boolean;
		/** Why the first read failed: 0 when it never reached the server, else the status the server answered. */
		failure?: number | null;
		stale?: boolean;
		onTab: (tab: ActivityTab) => void;
		onOpen: (key: string) => void;
		onDismiss: (key: string) => void;
		onMarkAllRead: () => void;
		onOpenRanker: () => void;
		onRetry: () => void;
	} = $props();

	const items = $derived(tab === 'tasks' ? tasks : notifications);
	const orderedAt = $derived(rankedAt(tasks));
	const rankerName = $derived(ranker?.label ?? null);
	let listEl = $state<HTMLElement | undefined>(undefined);
	let tabsEl = $state<HTMLElement | undefined>(undefined);

	const TABS: { id: ActivityTab; label: string }[] = [
		{ id: 'tasks', label: 'Your tasks' },
		{ id: 'notifications', label: 'Notifications' },
	];
	const countOf = (id: ActivityTab) => (id === 'tasks' ? taskCount : notificationCount);
	/** An empty tab stays, with no count. */
	const shownCount = (id: ActivityTab) => (loading || countOf(id) === 0 ? null : countOf(id));

	/** Left and Right move between the tabs and show the one they land on. */
	async function onTabKeydown(event: KeyboardEvent) {
		if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
		event.preventDefault();
		const to: ActivityTab = tab === 'tasks' ? 'notifications' : 'tasks';
		onTab(to);
		await tick();
		tabsEl?.querySelector<HTMLElement>(`[data-tab="${to}"]`)?.focus();
	}

	/** Up and Down move between rows, Home and End to the ends; Enter or Space opens (the row is a button). */
	async function onRowKeydown(event: KeyboardEvent, index: number) {
		let to: number | null = null;
		if (event.key === 'ArrowDown') to = Math.min(items.length - 1, index + 1);
		else if (event.key === 'ArrowUp') to = Math.max(0, index - 1);
		else if (event.key === 'Home') to = 0;
		else if (event.key === 'End') to = items.length - 1;
		if (to === null) return;
		event.preventDefault();
		await tick();
		listEl?.querySelectorAll<HTMLElement>('[data-activity-row]')[to]?.focus();
	}

	/** Puts focus on the open row, for after an act moves it. */
	export function focusOpen() {
		listEl?.querySelector<HTMLElement>('[data-activity-row][aria-current="true"]')?.focus();
	}

	// A phone's swipe: a row follows the finger left, and a full swipe dismisses it at once.
	let swipe = $state<{ key: string; startX: number; startY: number; dx: number; pointer: number } | null>(null);
	const SWIPE_DISMISS = 0.5;
	function swipeStart(event: PointerEvent, key: string) {
		if (event.pointerType !== 'touch') return;
		swipe = { key, startX: event.clientX, startY: event.clientY, dx: 0, pointer: event.pointerId };
	}
	function swipeMove(event: PointerEvent) {
		if (!swipe || event.pointerId !== swipe.pointer) return;
		const dx = Math.min(0, event.clientX - swipe.startX);
		// A mostly vertical move is the list scrolling, not a swipe.
		if (swipe.dx === 0 && Math.abs(event.clientY - swipe.startY) > Math.abs(dx)) { swipe = null; return; }
		swipe = { ...swipe, dx };
	}
	function swipeEnd(event: PointerEvent) {
		if (!swipe || event.pointerId !== swipe.pointer) return;
		const width = (event.currentTarget as HTMLElement).offsetWidth || 1;
		const { key, dx } = swipe;
		swipe = null;
		if (-dx / width >= SWIPE_DISMISS) onDismiss(key);
	}
	const swiped = (key: string) => (swipe?.key === key && swipe.dx < 0 ? `transform: translateX(${swipe.dx}px)` : undefined);
</script>

<div class="activity-list" aria-busy={loading}>
	<div class="tabs" role="tablist" aria-label="Activity" bind:this={tabsEl}>
		{#each TABS as { id, label } (id)}
			{@const count = shownCount(id)}
			<button
				type="button"
				role="tab"
				class="tab"
				data-tab={id}
				id="activity-tab-{id}"
				aria-selected={tab === id}
				aria-controls="activity-panel"
				aria-label={count === null ? label : `${label}, ${count}`}
				tabindex={tab === id ? 0 : -1}
				onclick={() => onTab(id)}
				onkeydown={onTabKeydown}
			>{label}{#if count !== null}<span class="count">{count}</span>{/if}</button>
		{/each}
	</div>
	<div class="panel" id="activity-panel" role="tabpanel" aria-labelledby="activity-tab-{tab}">
		{#if stale}<p class="by" role="status">Showing last known items.</p>{/if}
		{#if notice}<p class="by notice" role="status">{notice}</p>{/if}
		{#if failure !== null}
			<div class="empty" role="alert">
				<b>Activity couldn’t load.</b>
				{#if failure === 0}<p>Check your connection, then try again.</p>{/if}
				<button type="button" class="link" onclick={onRetry}>Try again</button>
			</div>
		{:else if !loading && items.length === 0}
			<p class="empty-line">{EMPTY_LINE[tab]}</p>
		{:else}
			{#if !loading}
				<div class="by">
					{#if tab === 'notifications'}
						<button type="button" class="link mark-all" disabled={markingAll} onclick={onMarkAllRead}>Mark all read</button>
					{:else if rankerName && orderedAt}
						<span class="by-text">Order set by <button type="button" class="link" onclick={onOpenRanker}>{rankerName}</button> at {formatTime(orderedAt)}</span>
					{:else if rankerName}
						<span class="by-text">Not ranked yet. <button type="button" class="link" onclick={onOpenRanker}>{rankerName}</button> sets the order.</span>
					{:else}
						<span class="by-text">Oldest first.</span>
					{/if}
				</div>
				{#if tab === 'notifications' && markAllFailed}<p class="failed" role="alert">{markAllFailed}</p>{/if}
			{/if}
			{#if loading}
				<div class="rows" aria-hidden="true">
					{#each [0, 1, 2, 3, 4] as row (row)}
						<div class="item ghost"><span class="bar wide"></span><span class="bar"></span></div>
					{/each}
				</div>
			{:else}
				<ol class="rows" class:notes={tab === 'notifications'} bind:this={listEl} aria-label={tab === 'tasks' ? 'Your tasks' : 'Notifications'}>
					{#each items as item, index (item.key)}
						{@const open = item.key === openKey}
						{@const doc = documentDetailOf(item)}
						{@const replies = reviewOf(item)?.unread_replies ?? 0}
						<li
							class="row"
							onpointerdown={doc ? (event) => swipeStart(event, item.key) : undefined}
							onpointermove={doc ? swipeMove : undefined}
							onpointerup={doc ? swipeEnd : undefined}
							onpointercancel={() => { swipe = null; }}
						>
							<button
								type="button"
								class="item"
								class:sel={open}
								style={swiped(item.key)}
								data-activity-row
								aria-current={open ? 'true' : undefined}
								onclick={() => onOpen(item.key)}
								onkeydown={(event) => onRowKeydown(event, index)}
							>
								{#if doc}
									<strong class="one">{item.title}</strong>
									<span class="sub">{item.key === allReadKey ? 'All replies read' : [unreadRepliesLabel(doc.unread), latestLabel(item.raised_at)].filter(Boolean).join(' · ')}</span>
								{:else}
									<span class="itemtop">
										<span class="rank">{index + 1}</span>
										<strong class:one={!open}>{item.title}</strong>
									</span>
									<span class="sub indent">{item.source.thread_label} · {KIND_LABEL[item.kind]}{item.raised_at ? ` · ${ageLabel(item.raised_at)}` : ''}{replies ? ` · ${unreadRepliesLabel(replies)}` : ''}</span>
									{#if open}
										<span class="why">{item.rank ? `Why it’s here: ${item.rank.reason}` : 'Not ranked yet'}</span>
									{/if}
								{/if}
							</button>
							{#if doc && item.key !== allReadKey}
								<button type="button" class="dismiss" aria-label="Mark {item.title} read" title="Mark read" onclick={() => onDismiss(item.key)}>
									<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
								</button>
							{/if}
							{#if dismissFailed[item.key]}<p class="failed row-failed" role="alert">{dismissFailed[item.key]}</p>{/if}
						</li>
					{/each}
				</ol>
			{/if}
		{/if}
	</div>
</div>

<style>
	/* Rank numbers sit in their own column, wide enough for three digits; what follows a title lines up with the title.
	   Tab labels, the order line, the empty line and a notification's text start on the row text's left edge. */
	.activity-list {
		--rank-col: 3ch; --title-indent: calc(var(--rank-col) + var(--space-2));
		--edge: var(--space-2);
		padding: 0 var(--space-3) var(--space-4);
	}
	.tabs {
		position: sticky; top: 0; z-index: 1; background: var(--bg-primary);
		display: flex; gap: var(--space-5); padding: calc(var(--space-3) + var(--space-1)) var(--edge) 0;
		border-bottom: 1px solid var(--border-subtle);
	}
	.tab {
		display: inline-flex; align-items: baseline; gap: var(--space-2); padding: var(--space-2) 0;
		border: 0; border-bottom: 1px solid transparent; background: none; cursor: pointer;
		font: inherit; font-size: var(--text-subhead); font-weight: var(--fw-medium); color: var(--text-secondary);
	}
	.tab[aria-selected='true'] { color: var(--text-primary); border-bottom-color: var(--accent); }
	.tab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.count { color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
	.link {
		padding: 0; border: 0; background: none; font: inherit; color: var(--accent-text); cursor: pointer;
	}
	.link:disabled { color: var(--text-tertiary); cursor: default; }
	.by {
		display: flex; align-items: baseline; gap: var(--space-2); padding: var(--space-3) var(--edge) var(--space-2);
		color: var(--text-tertiary); font-size: var(--text-footnote);
	}
	p.by { margin: 0; }
	.by-text { min-width: 0; }
	.mark-all { margin-left: auto; flex: none; font-weight: var(--fw-medium); }
	.failed { margin: 0 var(--edge) var(--space-2); color: var(--red-text); font-size: var(--text-footnote); }
	.rows { margin: 0; padding: 0; list-style: none; }
	.row { position: relative; touch-action: pan-y; }
	.item {
		display: block; width: 100%; padding: var(--space-2) var(--edge); border: 0; border-radius: var(--radius-sm);
		background: none; color: var(--text-primary); font: inherit; text-align: left; cursor: pointer;
	}
	.notes .item { padding-right: calc(var(--space-6) + var(--space-2)); }
	.item.sel { background: var(--accent-wash); }
	@media (hover: hover) { .item:not(.sel):not(.ghost):hover { background: var(--bg-hover); } }
	.item:focus-visible, .link:focus-visible, .dismiss:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.itemtop { display: flex; gap: var(--space-2); align-items: baseline; min-width: 0; }
	.rank {
		flex: none; width: var(--rank-col); text-align: right; color: var(--text-tertiary);
		font-size: var(--text-footnote); font-variant-numeric: tabular-nums;
	}
	strong { display: block; min-width: 0; overflow-wrap: anywhere; font-size: var(--text-subhead); line-height: 1.28; font-weight: var(--fw-medium); }
	strong.one { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.sub { display: block; padding: var(--space-px-3) 0 0; color: var(--text-tertiary); font-size: var(--text-caption); }
	.sub.indent { padding-left: var(--title-indent); }
	.why { display: block; padding: var(--space-2) 0 var(--space-px-2) var(--title-indent); color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.4; }
	/* × shows when a pointer is over the row; a keyboard reaches it too. */
	.dismiss {
		position: absolute; top: 50%; right: var(--space-2); transform: translateY(-50%);
		display: inline-flex; align-items: center; justify-content: center; width: var(--space-6); height: var(--space-6);
		padding: 0; border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-tertiary); cursor: pointer;
		opacity: 0;
	}
	.row:hover .dismiss, .dismiss:focus-visible { opacity: 1; }
	.dismiss:hover { background: var(--bg-hover); color: var(--text-primary); }
	.row-failed { margin-top: 0; padding-left: 0; }
	.ghost { cursor: default; }
	.ghost .bar { display: block; height: var(--space-2); width: 45%; margin: var(--space-2) 0 var(--space-2) var(--title-indent); border-radius: var(--radius-xs); background: var(--bg-hover); }
	.ghost .bar.wide { width: 80%; height: var(--space-3); }
	.empty { padding: calc(var(--space-7) + var(--space-2)) var(--edge) var(--space-2); }
	.empty b { font-size: var(--text-subhead); font-weight: var(--fw-semibold); }
	.empty p { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: 1.45; }
	.empty-line { margin: 0; padding: var(--space-4) var(--edge); color: var(--text-tertiary); font-size: var(--text-subhead); }
	/* On a phone (Activity's own phone mode) tabs, rows and links grow to touch size, and × gives way to the swipe.
	   Titles keep the sidebar rows' size, which the phone type scale already raises. A link's touch area is drawn
	   around it, so the header lines keep their spacing. */
	:global(.activity.phone) .tab { min-height: var(--touch-target); align-items: center; }
	:global(.activity.phone) .item { padding: var(--space-3) var(--edge); }
	:global(.activity.phone) .notes .item { padding-right: var(--edge); transition: transform 0.12s ease-out; }
	:global(.activity.phone) .dismiss { display: none; }
	:global(.activity.phone) .link { position: relative; }
	/* Reserve the full target in the flow so the first row cannot cover it. */
	:global(.activity.phone) .by { min-height: var(--touch-target); padding-block: 0; align-items: center; }
	:global(.activity.phone) .link::after {
		content: ''; position: absolute; left: 0; right: 0; top: 50%; height: var(--touch-target); transform: translateY(-50%);
	}
</style>
