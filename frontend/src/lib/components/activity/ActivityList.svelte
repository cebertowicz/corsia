<script lang="ts">
	import { tick } from 'svelte';
	import type { ActivityQueueItem, ActivityRanker } from '$lib/api/activityQueue';
	import { formatTime } from '$lib/time';
	import { KIND_LABEL, ageLabel, rankedAt } from './activityItems';

	/**
	 * Everything waiting on the person, one line per item in the ranker's order. The open item shows its full title
	 * and why it sits there; Next moves on without acting. While the first read is in flight the header shows over
	 * greyed rows.
	 */
	let {
		items,
		count,
		ranker,
		openKey,
		loading = false,
		failed = false,
		nextDisabled,
		onOpen,
		onNext,
		onOpenRanker,
		onRetry,
	}: {
		items: readonly ActivityQueueItem[];
		count: number;
		ranker: ActivityRanker | null;
		/** The open item, or on a phone the marked one. */
		openKey: string | null;
		loading?: boolean;
		failed?: boolean;
		nextDisabled: boolean;
		onOpen: (key: string) => void;
		onNext: () => void;
		onOpenRanker: () => void;
		onRetry: () => void;
	} = $props();

	const orderedAt = $derived(rankedAt(items));
	const rankerName = $derived(ranker?.label ?? null);
	let listEl = $state<HTMLElement | undefined>(undefined);

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

	/** Puts focus on the open row, for after an act or Next moves it. */
	export function focusOpen() {
		listEl?.querySelector<HTMLElement>('[data-activity-row][aria-current="true"]')?.focus();
	}
</script>

<div class="activity-list" aria-busy={loading}>
	{#if failed}
		<div class="empty" role="alert">
			<b>Activity couldn’t load.</b>
			<p>Check your connection, then try again.</p>
			<button type="button" class="link" onclick={onRetry}>Try again</button>
		</div>
	{:else if !loading && items.length === 0}
		<div class="empty">
			<b>Nothing is waiting on you.</b>
			<p>When an agent needs you, it shows here{#if rankerName}, in the order {rankerName} sets{/if}.</p>
		</div>
	{:else}
		<div class="ranked">
			<div class="rtop">
				<h2>Waiting on you</h2>
				{#if !loading}<span class="count">{count}</span>{/if}
				<button type="button" class="next link" disabled={loading || nextDisabled} onclick={onNext}>Next ›</button>
			</div>
			{#if !loading}
				<div class="by">
					{#if rankerName && orderedAt}
						Order set by <button type="button" class="link" onclick={onOpenRanker}>{rankerName}</button> at {formatTime(orderedAt)}
					{:else if rankerName}
						Not ranked yet. <button type="button" class="link" onclick={onOpenRanker}>{rankerName}</button> sets the order.
					{:else}
						Oldest first.
					{/if}
				</div>
			{/if}
		</div>
		{#if loading}
			<div class="rows" aria-hidden="true">
				{#each [0, 1, 2, 3, 4] as row (row)}
					<div class="item ghost"><span class="bar wide"></span><span class="bar"></span></div>
				{/each}
			</div>
		{:else}
			<ol class="rows" bind:this={listEl} aria-label="Waiting on you">
				{#each items as item, index (item.key)}
					{@const open = item.key === openKey}
					<li>
						<button
							type="button"
							class="item"
							class:sel={open}
							data-activity-row
							aria-current={open ? 'true' : undefined}
							onclick={() => onOpen(item.key)}
							onkeydown={(event) => onRowKeydown(event, index)}
						>
							<span class="itemtop">
								<span class="rank">{index + 1}</span>
								<strong class:one={!open}>{item.title}</strong>
							</span>
							<span class="sub">{item.source.thread_label} · {KIND_LABEL[item.kind]}{item.raised_at ? ` · ${ageLabel(item.raised_at)}` : ''}</span>
							{#if open}
								<span class="why">{item.rank ? `Why it’s here: ${item.rank.reason}` : 'Not ranked yet'}</span>
							{/if}
						</button>
					</li>
				{/each}
			</ol>
		{/if}
	{/if}
	<p class="nothere">Not gathered here: questions asked inside chat messages, and email. Open that chat to answer them.</p>
</div>

<style>
	/* Rank numbers sit in their own column; what follows a title lines up with the title. */
	.activity-list {
		--rank-col: var(--space-5); --title-indent: calc(var(--rank-col) + var(--space-2));
		padding: var(--space-1) var(--space-3) var(--space-6);
	}
	.ranked { padding: var(--space-3) var(--space-2) var(--space-4); }
	.rtop { display: flex; align-items: baseline; gap: var(--space-2); }
	.rtop h2 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-primary); }
	.count { color: var(--text-tertiary); font-size: var(--text-subhead); font-weight: var(--fw-medium); font-variant-numeric: tabular-nums; }
	.link {
		padding: 0; border: 0; background: none; font: inherit; color: var(--accent-text); cursor: pointer;
	}
	.link:disabled { color: var(--text-tertiary); cursor: default; }
	.next { margin-left: auto; font-size: var(--text-subhead); font-weight: var(--fw-medium); }
	.by { margin-top: var(--space-1); color: var(--text-tertiary); font-size: var(--text-footnote); }
	.rows { margin: 0; padding: 0; list-style: none; }
	.item {
		display: block; width: 100%; padding: var(--space-2); border: 0; border-radius: var(--radius-sm);
		background: none; color: var(--text-primary); font: inherit; text-align: left; cursor: pointer;
	}
	.item.sel { background: var(--accent-wash); }
	@media (hover: hover) { .item:not(.sel):not(.ghost):hover { background: var(--bg-hover); } }
	.item:focus-visible, .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.itemtop { display: flex; gap: var(--space-2); align-items: baseline; min-width: 0; }
	.rank {
		flex: none; width: var(--rank-col); text-align: right; color: var(--text-tertiary);
		font-size: var(--text-footnote); font-variant-numeric: tabular-nums;
	}
	.itemtop strong { min-width: 0; font-size: var(--text-subhead); line-height: 1.28; font-weight: var(--fw-medium); }
	.itemtop strong.one { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.sub { display: block; padding: var(--space-px-3) 0 0 var(--title-indent); color: var(--text-tertiary); font-size: var(--text-caption); }
	.why { display: block; padding: var(--space-2) 0 var(--space-px-2) var(--title-indent); color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.4; }
	.ghost { cursor: default; }
	.ghost .bar { display: block; height: var(--space-2); width: 45%; margin: var(--space-2) 0 var(--space-2) var(--title-indent); border-radius: var(--radius-xs); background: var(--bg-hover); }
	.ghost .bar.wide { width: 80%; height: var(--space-3); }
	.empty { padding: calc(var(--space-7) + var(--space-2)) var(--space-2) var(--space-2); }
	.empty b { font-size: var(--text-subhead); font-weight: var(--fw-semibold); }
	.empty p { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: 1.45; }
	.nothere { margin: var(--space-4) var(--space-2); color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.4; }
	/* On a phone (Activity's own phone mode) rows and links grow to touch size. Titles keep the sidebar rows' size, which
	   the phone type scale already raises. A link's touch area is drawn around it, so the header lines keep their spacing. */
	:global(.activity.phone) .item { padding: var(--space-3) var(--space-2); }
	:global(.activity.phone) .link { position: relative; }
	:global(.activity.phone) .link::after {
		content: ''; position: absolute; left: 0; right: 0; top: 50%; height: var(--touch-target); transform: translateY(-50%);
	}
</style>
