<script lang="ts">
	import { tick } from 'svelte';
	import type { OperatorKey } from '$lib/stores/planKeys';
	import { planQuery } from '$lib/stores/planQuery.svelte';
	import type { ActivityPlanRow } from '$lib/activityHome';
	import Button from '$lib/components/ui/Button.svelte';
	import PlanTree from '$lib/components/plans/PlanTree.svelte';
	import PlanDrillView from '$lib/components/plans/PlanDrillView.svelte';
	import { foldedOf } from '$lib/components/plans/planFoldLevels';
	import { createChildExpansion } from '$lib/components/plans/childExpansion.svelte';
	import { cardStateLabel, cardStateTone } from '$lib/components/plans/planDisplay';
	import { openPlanAsk } from '$lib/planLeafReturn';
	import ActivityIdentityGlyph from './ActivityIdentityGlyph.svelte';
	import ThreadWaitingMark from '$lib/components/ThreadWaitingMark.svelte';
	import SnoozeClock from '$lib/components/plans/SnoozeClock.svelte';
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import { setAsideLine, waitBack, waitChecksBack } from '$lib/components/plans/planWaits';
	import { openWaitArtifact, UNOPENABLE_DOCUMENT } from '$lib/openWaitArtifact';
	import { minuteClock } from '$lib/time';

	let {
		row,
		onSelectThread,
		onOpenDecision = () => {},
		onOpenChild = () => {},
		onOpenLegacyDiscussion = () => {},
	}: {
		row: ActivityPlanRow;
		onSelectThread: (threadId: string) => void | Promise<void>;
		/** Opens this row's thread with its decision expanded. */
		onOpenDecision?: (threadId: string) => void | Promise<void>;
		onOpenChild?: (
			parentThreadId: string,
			nodeId: string | null,
			childThreadId: string,
			title: string,
			stepId?: string,
		) => void | Promise<void>;
		onOpenLegacyDiscussion?: (
			parentThreadId: string,
			artifactId: string,
			title: string,
		) => void | Promise<void>;
	} = $props();

	let expanded = $state(false);
	let drillRootId = $state<string | null>(null);
	let detailElement = $state<HTMLDivElement | null>(null);
	let focusTreeAfterRetry = $state(false);
	let staleRefreshRevision = $state<number | null>(null);
	const threadId = $derived(row.entry.owner.thread_id!);
	const hasPlan = $derived(row.entry.has_nodes);
	const detailHostKey: OperatorKey = $derived({ kind: 'thread', id: threadId });
	const detailKey: OperatorKey | null = $derived(expanded && hasPlan ? detailHostKey : null);
	const detailQuery = planQuery(
		() => detailKey,
		{ retireEmptyPendingOnRelease: true },
	);
	const detailEntry = $derived(detailQuery.payload?.plans[0] ?? null);
	const childExpansion = createChildExpansion(() => detailHostKey);
	const detailProblem = $derived(
		detailQuery.status === 'stale'
		|| detailQuery.status === 'error'
		|| detailQuery.status === 'not-found'
		|| detailQuery.status === 'forbidden'
		|| detailQuery.status === 'unauthorized',
	);

	// State vocabulary and tone come straight from the server projection.
	const tone = $derived(cardStateTone(row.entry));
	const stateLabel = $derived(cardStateLabel(row.state));
	// The plan waits on something outside: what, and when its agent checks back (EW7). The server's thread
	// state carries it; the activity index does not.
	const wait = $derived.by(() => {
		const entry = $threadPlanStates[threadId];
		return entry?.state === 'waiting' ? entry.wait ?? null : null;
	});
	// Every wait is set aside by its agent: the plan comes back at a time it chose (EW7); no state word.
	const setAside = $derived.by(() => {
		const entry = $threadPlanStates[threadId];
		// Only an idle plan reads set aside: one still working or owing you keeps its own state.
		return !wait && entry?.state === 'idle' && entry.set_aside_until ? setAsideLine(entry.set_aside_until, $minuteClock) : null;
	});
	// A host-like label ("github.com/…/checks") stays as written; any other starts a sentence.
	const waitLabel = $derived(!wait ? '' : /^[\w-]+(\.[\w-]+)+(\/|$)/.test(wait.label) ? wait.label
		: wait.label.charAt(0).toUpperCase() + wait.label.slice(1));
	const waitMore = $derived(wait && wait.more > 0 ? ` +${wait.more} more` : '');
	const waitBackText = $derived(wait ? waitBack(wait.until, $minuteClock) : '');
	const waitTitle = $derived(wait ? [`Waiting for ${wait.label}${waitMore}`, waitChecksBack(wait.until, $minuteClock)].filter(Boolean).join(' · ') : '');

	// A document that fails to open stops being a link, and its title says so.
	let unopenable = $state<string | null>(null);
	const waitLinked = $derived(wait?.artifact_id && unopenable !== wait.artifact_id ? wait.artifact_id : null);

	async function openWait(): Promise<void> {
		if (!waitLinked || !wait) return;
		const artifactId = waitLinked;
		if (!(await openWaitArtifact(artifactId, wait.label, threadId))) unopenable = artifactId;
	}

	const STATE_SYMBOL: Record<string, string> = {
		'human-review': '!',
		waiting: '•',
		complete: '✓',
		failed: '×',
		closed: '–',
	};

	function openAsk(artifact: import('$lib/api/plans').OperatorArtifactLink, nodeTitle: string): void {
		openPlanAsk(threadId, artifact, nodeTitle, () => { void onOpenDecision(threadId); });
	}

	$effect(() => {
		if (!expanded || !detailEntry || detailQuery.refreshing) return;
		if (detailEntry.plan.revision >= row.entry.plan.revision) return;
		if (staleRefreshRevision === row.entry.plan.revision) return;
		staleRefreshRevision = row.entry.plan.revision;
		detailQuery.retry();
	});

	$effect(() => {
		if (!focusTreeAfterRetry || detailQuery.refreshing) return;
		if (detailQuery.status !== 'ready' || !detailEntry || !detailElement) {
			focusTreeAfterRetry = false;
			return;
		}
		focusTreeAfterRetry = false;
		void tick().then(() => {
			detailElement?.querySelector<HTMLElement>('button.row-body')?.focus();
		});
	});

	async function activateSummary() {
		if (hasPlan) {
			expanded = !expanded;
			if (!expanded) {
				drillRootId = null;
				focusTreeAfterRetry = false;
				staleRefreshRevision = null;
			}
			return;
		}
		await onSelectThread(threadId);
	}

	function retryDetail(event: MouseEvent) {
		focusTreeAfterRetry = document.activeElement === event.currentTarget;
		detailQuery.retry();
	}
</script>

<article class="plan-row" class:expanded>
	<div class="row-head">
		<!-- The row's toggle is a button laid under its content, so the wait's label link is its own
		     button beside it, never inside it. -->
		<div class="row-main">
			<button
				class="row-summary"
				onclick={activateSummary}
				aria-expanded={hasPlan ? expanded : undefined}
				aria-label={hasPlan
					? (expanded ? 'Collapse ' : 'Expand ') + row.entry.owner.label
					: 'Open ' + row.entry.owner.label}
			></button>
			<div class="row-content">
				<span class="owner-mark" aria-hidden="true"><ActivityIdentityGlyph kind={row.ownerType} /></span>
				<span class="owner-copy">
					<strong aria-hidden="true">{row.entry.owner.label}</strong>
					{#if wait}
						<!-- One line at every width: only the label truncates, and the time always shows. -->
						<span class="wait" title={waitTitle}>{#if waitLinked}<button type="button" class="wait-label wait-link" onclick={openWait}>{waitLabel}{waitMore}</button>{:else}<span class="wait-label" title={wait.artifact_id ? UNOPENABLE_DOCUMENT : undefined}>{waitLabel}{waitMore}</span>{/if}{#if waitBackText}<span class="wait-when">{' · '}{waitBackText}</span>{/if}</span>
					{:else if setAside}
						<span class="set-aside">{setAside}</span>
					{:else}
						<span>{row.ownerType === 'agent' ? 'Agent' : 'Thread'}</span>
					{/if}
				</span>
				<!-- One group, so a narrow row can set the state under the name instead of
				     squeezing the name; a wide row lays the parts out inline. -->
				<span class="row-meta">
					<span
						class="plan-state {tone}"
						class:spinning={row.entry.progress.liveness === 'advancing'}
						role="img"
						aria-label={stateLabel}
						title={stateLabel}
					>{#if wait}<ThreadWaitingMark size={12} />{:else if setAside}<SnoozeClock />{:else}{STATE_SYMBOL[tone] ?? ''}{/if}</span>
					{#if !setAside}<span class="state-word {tone}">{stateLabel}</span>{/if}
					<!-- Failure state is carried by the canonical plan display above. -->
					{#if row.entry.progress.open > 0}
						<span class="progress">{row.entry.progress.open} {row.entry.progress.open === 1 ? 'task' : 'tasks'} left</span>
					{/if}
				</span>
				<!-- A wide row expands with this chevron and opens with the Open button.
				     A narrow row has one affordance: the whole row, with this chevron
				     (down/up when it expands, forward when it opens). -->
				<span class="chevron" class:expanded class:opens={!hasPlan} aria-hidden="true">›</span>
			</div>
		</div>
		<span class="row-open">
			<Button
				variant="ghost"
				size="sm"
				title={'Open ' + row.entry.owner.label}
				onclick={() => onSelectThread(threadId)}
			>
				Open
			</Button>
		</span>
	</div>

	{#if expanded}
		<div class="plan-detail" bind:this={detailElement}>
			<div class="detail-open">
				<Button
					variant="ghost"
					size="sm"
					title={'Open ' + row.entry.owner.label}
					onclick={() => onSelectThread(threadId)}
				>
					Open {row.ownerType === 'agent' ? 'agent' : 'thread'}
				</Button>
			</div>
			{#if detailEntry}
				{#if drillRootId}
					<PlanDrillView
						nodes={detailEntry.nodes}
						folded={foldedOf(detailEntry)}
						rootId={drillRootId}
						currentNodeIds={detailEntry.current_node_ids}
						nextNodeIds={detailEntry.next_node_ids}
						hostKey={detailHostKey}
						onBack={() => (drillRootId = null)}
						onOpenChild={(nodeId, childThreadId, title, stepId) =>
							onOpenChild(threadId, nodeId, childThreadId, title, stepId)}
						onOpenArtifact={openAsk}
						onOpenLegacyDiscussion={(artifactId, title) =>
							onOpenLegacyDiscussion(threadId, artifactId, title)}
					/>
				{:else}
					<PlanTree
						nodes={detailEntry.nodes}
						folded={foldedOf(detailEntry)}
						currentNodeIds={detailEntry.current_node_ids}
						nextNodeIds={detailEntry.next_node_ids}
						isChildExpanded={childExpansion.isExpanded}
						childRowState={childExpansion.state}
						onToggleChild={childExpansion.toggle}
						onRetryChild={childExpansion.retry}
						onDrill={(nodeId) => (drillRootId = nodeId)}
						onOpenChild={(nodeId, childThreadId, title, stepId) =>
							onOpenChild(threadId, nodeId, childThreadId, title, stepId)}
						onOpenArtifact={openAsk}
						onOpenLegacyDiscussion={(artifactId, title) =>
							onOpenLegacyDiscussion(threadId, artifactId, title)}
					/>
				{/if}
			{/if}
			{#if hasPlan && detailProblem}
				<div class="detail-error" role="alert">
					<span>Couldn't load this plan.</span>
					<Button variant="ghost" size="sm" onclick={retryDetail}>Retry</Button>
				</div>
			{:else if hasPlan && !detailEntry}
				<div class="detail-loading" aria-busy="true">
					<span class="sr-only">Loading plan…</span>
				</div>
			{/if}
		</div>
	{/if}
</article>

<style>
	.plan-row { border-bottom: 1px solid var(--border-subtle); }
	.plan-row:last-child { border-bottom: 0; }
	.row-head { display: flex; align-items: center; gap: var(--space-1); min-height: 54px; }
	.row-main { position: relative; display: flex; flex: 1; min-width: 0; }
	/* The toggle lies under the row's content; a tap anywhere but the wait link reaches it. */
	.row-summary {
		position: absolute; inset: 0; width: 100%; padding: 0;
		border: 0; background: transparent; cursor: pointer;
	}
	.row-content {
		display: flex; flex: 1; min-width: 0; align-items: center; gap: var(--space-2);
		min-height: 54px; padding: var(--space-2) 0; pointer-events: none;
	}
	.row-content .wait-link { position: relative; z-index: 1; pointer-events: auto; }
	/* Hover colour is for a pointer. A touch tap leaves :hover stuck on the last
	   row tapped, which read as a different state. */
	@media (hover: hover) {
		.row-summary:hover + .row-content .owner-copy strong { color: var(--accent-text); }
	}
	.row-meta, .row-open { display: contents; }
	.row-summary:focus-visible {
		outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs);
	}
	.owner-mark {
		display: inline-flex; width: 28px; height: 28px; flex: none; align-items: center; justify-content: center;
		border: 1px solid var(--border); border-radius: var(--radius-circle);
		color: var(--text-secondary); font-size: var(--text-caption); font-weight: var(--fw-semibold);
	}
	.owner-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; }
	.owner-copy strong {
		overflow: hidden; color: var(--text-primary); font-size: var(--text-subhead);
		font-weight: var(--fw-medium); text-overflow: ellipsis; white-space: nowrap;
	}
	.owner-copy > span { color: var(--text-tertiary); font-size: var(--text-caption); }
	.owner-copy > .wait { display: flex; min-width: 0; white-space: nowrap; }
	.wait-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.wait-when { flex: none; white-space: pre; }
	.wait-link { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; text-decoration: underline; cursor: pointer; }
	.wait-link:hover { color: var(--text-primary); }
	.wait-link:focus-visible { outline: 2px solid var(--accent); outline-offset: -1px; border-radius: var(--radius-xs); }
	.plan-state {
		display: inline-flex; width: 18px; height: 18px; flex: none; align-items: center; justify-content: center;
		border: 1px solid var(--border); border-radius: var(--radius-circle);
		color: var(--text-secondary); font-size: var(--text-caption); font-weight: var(--fw-bold); line-height: 1;
	}
	.plan-state.active {
		border-color: var(--status-in-progress); border-top-color: transparent;
		color: var(--status-in-progress);
	}
	.plan-state.spinning { animation: activity-plan-spin 1.2s linear infinite; }
	.plan-state.failed { border-color: var(--red); color: var(--red); }
	.plan-state.human-review { border-color: var(--amber); color: var(--amber); }
	.plan-state.waiting, .plan-state.closed { border-color: var(--border); color: var(--text-tertiary); }
	.plan-state.complete { border-color: var(--green); background: var(--green); color: var(--bg-primary); }
	.state-word {
		flex: none; color: var(--text-tertiary); font-size: var(--text-caption);
	}
	.state-word.failed { color: var(--red); }
	.state-word.human-review { color: var(--amber); }
	.progress {
		flex: none; color: var(--text-tertiary); font-size: var(--text-caption);
		font-variant-numeric: tabular-nums;
	}
	.chevron {
		flex: none; color: var(--text-tertiary); font-size: 20px;
		transform: rotate(0); transition: transform var(--dur-tight) ease;
	}
	.chevron.expanded { transform: rotate(90deg); }
	/* A row with nothing to expand is opened by its Open button on a wide row. */
	.chevron.opens { display: none; }
	.detail-open { display: none; }
	.plan-detail { padding: 0 0 var(--space-2) 36px; }
	.detail-loading { min-height: 1.5rem; }
	.detail-error {
		display: flex; min-height: 1.5rem; align-items: center; justify-content: space-between;
		gap: var(--space-2); color: var(--text-secondary); font-size: var(--text-caption);
	}
	.sr-only {
		position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
		overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
	}
	@keyframes activity-plan-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.plan-state.spinning { animation: none; }
	}
	/* Phone width: the name keeps the full line (two at most) and the state sits
	   under it; the whole row is the one tap target and carries the one chevron. */
	@media (max-width: 620px) {
		.row-head { min-height: 56px; }
		.row-open { display: none; }
		.row-content {
			display: grid; grid-template-columns: 28px minmax(0, 1fr) 20px;
			grid-template-areas: "mark copy chevron" ". meta meta";
			align-items: center; column-gap: var(--space-2); row-gap: 2px; min-height: 56px;
		}
		.owner-mark { grid-area: mark; }
		.owner-copy { grid-area: copy; }
		.owner-copy > span:not(.wait):not(.set-aside) { display: none; }
		.owner-copy strong {
			display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
			line-clamp: 2; white-space: normal; overflow-wrap: anywhere;
		}
		.row-meta {
			grid-area: meta; display: flex; flex-wrap: wrap; align-items: center;
			gap: 2px var(--space-2); min-width: 0;
		}
		.chevron { grid-area: chevron; justify-self: center; }
		.chevron.opens { display: inline; }
		.detail-open { display: block; padding-bottom: var(--space-1); }
		.detail-open :global(button) { min-height: 44px; }
		.plan-detail { padding-left: 18px; }
	}
</style>
