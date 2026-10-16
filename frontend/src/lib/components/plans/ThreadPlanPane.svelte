<script lang="ts">
	import { tick, untrack } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import PlanTree from './PlanTree.svelte';
	import PlanDrillView from './PlanDrillView.svelte';
	import { requestFocus } from '$lib/stores/attentionDock.svelte';
	import { createChildExpansion } from './childExpansion.svelte';
	import { createPaneView } from './paneView.svelte';
	import { foldedOf, hasFolded } from './planFoldLevels';
	import { planQuery } from '$lib/stores/planQuery.svelte';
	import { connectionState } from '$lib/stores/connection';
	import { token } from '$lib/stores/auth';
	import { activeThreadId } from '$lib/stores/threads';
	import { visibleQueue } from '$lib/stores/attentionDock.svelte';
	import type { AttentionQueueItem } from '$lib/api/attentionQueue';
	import { mutateUserTask } from '$lib/api/plans';
	import type { OperatorArtifactLink, OperatorNode, OperatorUserAction } from '$lib/api/plans';
	import type { OperatorKey } from '$lib/stores/planKeys';
	import { registerNodeFocus } from '$lib/planNodeFocus';
	import { openPlanAsk } from '$lib/planLeafReturn';

	let {
		threadId,
		onOpenChild = () => {},
		onOpenLegacyDiscussion = () => {},
	}: {
		threadId: string;
		onOpenChild?: (nodeId: string, threadId: string, title: string, stepId?: string) => void;
		onOpenLegacyDiscussion?: (artifactId: string, title: string) => void;
	} = $props();

	const key: OperatorKey = $derived({ kind: 'thread', id: threadId });
	const query = planQuery(() => key);
	const hasPayload = $derived(query.payload != null);
	const offline = $derived($connectionState !== 'connected' && hasPayload);
	const plan = $derived(query.payload?.plans[0] ?? null);
	const decisionCount = $derived(query.payload?.attention.awaiting_count ?? 0);
	/**
	 * This thread's own decisions, each a row, once the queue has them; otherwise the plan's
	 * awaiting count stands in. Each display reads one source, never a difference of the two.
	 */
	const activeDecisionItems = $derived(
		$activeThreadId === threadId
			? $visibleQueue.items.filter((item) => item.detail.source?.thread_id === threadId)
			: [],
	);
	/**
	 * Every step finished and folded (FW7): the empty state's calm, plus the
	 * folded count below it. Nothing is left, so the count is suppressed with
	 * the rest.
	 */
	const hasSteps = $derived(!!plan && (plan.nodes.length > 0 || hasFolded(plan.folds)));
	const allFolded = $derived(hasSteps && (plan?.nodes.every((node) => node.folded) ?? false));

	let drillRootId = $state<string | null>(null);
	let userActionBusy = $state(false);
	let decisionBusy = $state(false);
	let decisionMessage = $state<string | null>(null);
	const childExpansion = createChildExpansion(() => key);
	// What the user has seen of this plan here (`paneView`): only work new since then shows, then folds.
	const paneView = createPaneView({
		threadId: () => threadId,
		payload: () => query.payload ?? null,
		lastFreshAt: () => query.lastFreshAt,
	});
	/*
	 * Focusing an attention item's node (`planNodeFocus`). This pane is the
	 * only owner: it holds at most one node until its plan query first settles,
	 * then resolves it exactly once. A shown plan (fresh or last known) reveals it
	 * through the overview or the drill view and focuses its row; a plan that
	 * could not load says so and forgets it. A thread change drops it; a node the
	 * plan lacks says so. A step of a nested subagent plan (`stepId` set) focuses
	 * its row while it is rendered, else the step of this plan that nests it.
	 */
	const PLAN_UNLOADED = 'The plan couldn’t load, so that item can’t be shown';
	let pendingFocus = $state<{ nodeId: string; stepId: string | null } | null>(null);
	let focusNotice = $state<string | null>(null);
	let overviewTree = $state<PlanTree | null>(null);
	let drillView = $state<PlanDrillView | null>(null);

	$effect(() => registerNodeFocus(threadId, (nodeId, stepId) => {
		pendingFocus = { nodeId, stepId };
		focusNotice = null;
	}));

	$effect(() => {
		void threadId;
		untrack(() => {
			pendingFocus = null;
			focusNotice = null;
		});
	});

	$effect(() => {
		const request = pendingFocus;
		const status = query.status;
		if (!request || status === 'idle' || status === 'loading' || query.refreshing) return;
		pendingFocus = null;
		if (status === 'ready' || status === 'stale') untrack(() => void focusNode(request.nodeId, request.stepId));
		else focusNotice = PLAN_UNLOADED;
	});

	// A plan that loads after all makes "couldn’t load" untrue; the dropped node stays dropped.
	$effect(() => {
		if (plan) untrack(() => { if (focusNotice === PLAN_UNLOADED) focusNotice = null; });
	});

	async function focusNode(nodeId: string, stepId: string | null = null): Promise<void> {
		const node = plan?.nodes.find((candidate) => candidate.id === nodeId);
		if (!node) {
			// Not this plan's node: a nested subagent plan's row while rendered, else its owning step.
			if ((drillRootId ? drillView : overviewTree)?.focusRow(nodeId)) return;
			if (stepId && stepId !== nodeId) return focusNode(stepId);
			// Older finished work is not in the read: page it in from the server's folded history.
			const forThread = threadId;
			const drilled = drillRootId;
			drillRootId = null;
			await tick();
			if (threadId !== forThread) return;
			if (await overviewTree?.revealFolded(nodeId)) {
				await tick();
				if (threadId === forThread && !overviewTree?.focusRow(nodeId)) focusNotice = 'That item couldn’t be shown in this plan';
				return;
			}
			drillRootId = drilled;
			focusNotice = 'That item is no longer in this plan';
			return;
		}
		const forThread = threadId;
		drillRootId = null;
		await tick();
		if (threadId !== forThread) return;
		let tree: PlanTree | PlanDrillView | null = overviewTree;
		// Past the overview's depth the node shows the way a user reaches it: drilled into from its parent.
		if (!overviewTree?.revealNode(nodeId) && node.parent_id) {
			drillRootId = node.parent_id;
			await tick();
			drillView?.revealNode(nodeId);
			tree = drillView;
		}
		await tick();
		if (threadId !== forThread) return;
		if (!tree?.focusRow(nodeId)) focusNotice = 'That item couldn’t be shown in this plan';
	}


	function openAsk(artifact: OperatorArtifactLink, nodeTitle: string): void {
		openPlanAsk(threadId, artifact, nodeTitle, () => { void openDecisions(); });
	}

	function showDecisionOutcome(outcome: Awaited<ReturnType<typeof requestFocus>>): void {
		if (outcome === 'requested') return;
		if (outcome === 'empty') {
			decisionMessage = 'That item no longer needs you. Refreshing the plan.';
			query.retry();
			return;
		}
		decisionMessage = 'The item couldn’t open. Try again.';
	}

	async function openDecision(item: AttentionQueueItem): Promise<void> {
		if (decisionBusy) return;
		decisionBusy = true;
		decisionMessage = null;
		const outcome = await requestFocus(item.external_id);
		decisionBusy = false;
		showDecisionOutcome(outcome);
	}

	async function openDecisions(): Promise<void> {
		if (decisionBusy) return;
		decisionBusy = true;
		decisionMessage = null;
		const outcome = await requestFocus();
		decisionBusy = false;
		showDecisionOutcome(outcome);
	}

	/**
	 * Apply the user's control on a task, then re-read the plan. Returns the
	 * refusal, or null once it landed. The control the user clicked shows it
	 * beside itself (#2431), so the pane never repeats it at its top.
	 */
	async function applyUserAction(
		node: OperatorNode, action: OperatorUserAction, reason?: string,
	): Promise<string | null> {
		const current = plan;
		const bearer = $token;
		if (!current || !bearer || userActionBusy || offline) return 'Not available right now.';
		userActionBusy = true;
		const result = await mutateUserTask(
			bearer, current.plan.id, action, node.id, node.revision,
			`${action}:${node.id}:${node.revision}`, reason,
		);
		userActionBusy = false;
		if (result.status === 'success') {
			query.retry();
			return null;
		}
		let refusal: string;
		if (result.status === 'conflict') {
			// The server's own reason, not a generic line: it says what moved.
			refusal = result.detail ?? 'This task changed. Refreshing the latest state.';
			query.retry();
		} else {
			refusal = result.status === 'forbidden'
				? 'You no longer have permission to change this task.'
				: result.message;
		}
		return refusal;
	}
</script>

<div class="thread-plan-pane" tabindex="-1" data-drawer-autofocus>
	{#if focusNotice && !plan}
		<p class="decision-message" role="status">{focusNotice}</p>
	{/if}
	{#if query.status === 'loading' && !hasPayload}
		<div class="plan-state" aria-live="polite">
			<span class="spinner" aria-hidden="true"></span>
			<strong>Loading plan…</strong>
		</div>
	{:else if query.status === 'unauthorized' && !hasPayload}
		<div class="plan-state error" role="alert">
			<strong>Sign in to see this plan</strong>
			<p>Your session expired before the plan could load.</p>
		</div>
	{:else if query.status === 'forbidden' && !hasPayload}
		<div class="plan-state error" role="alert">
			<strong>This plan isn’t available</strong>
			<p>You don’t have access to this plan.</p>
		</div>
	{:else if query.status === 'not-found' && !hasPayload}
		<div class="plan-state error" role="alert">
			<strong>This plan isn’t available</strong>
			<p>It may have been removed or is no longer shared with you.</p>
		</div>
	{:else if query.status === 'error' && !hasPayload}
		<div class="plan-state error" role="alert">
			<strong>This plan couldn’t load</strong>
			<Button variant="secondary" size="sm" onclick={() => query.retry()}>Retry</Button>
		</div>
	{:else if plan}
		{#if hasPayload && (query.status === 'stale' || offline)}
			<div class="status-banner" role="status">
				<span>{offline ? "You're offline — showing last known data." : 'Showing last known data.'}</span>
				{#if !offline}<Button variant="ghost" size="sm" onclick={() => query.retry()}>Retry</Button>{/if}
			</div>
		{/if}
		{#if activeDecisionItems.length > 0}
			{#each activeDecisionItems as item (item.external_id)}
				<button class="decision-row" type="button" disabled={decisionBusy} onclick={() => openDecision(item)}>
					<span class="decision-mark" aria-hidden="true">!</span>
					<span class="decision-title">{item.detail.title}</span>
					<span class="decision-open">{decisionBusy ? 'Loading…' : 'Open'}</span>
				</button>
			{/each}
		{:else if decisionCount > 0}
			<button class="decision-indicator" disabled={decisionBusy} onclick={openDecisions}>
				<b>{decisionCount}</b> {decisionCount === 1 ? 'decision' : 'decisions'}
				<span>{decisionBusy ? 'Loading…' : 'Open'}</span>
			</button>
		{/if}
		{#if decisionMessage}
			<p class="decision-message" role="status">{decisionMessage}</p>
		{/if}
		{#if focusNotice}
			<p class="decision-message" role="status">{focusNotice}</p>
		{/if}
		{#if !hasSteps}
			<div class="plan-state empty"><strong>No plan steps yet</strong></div>
		{:else if drillRootId}
			<PlanDrillView
				bind:this={drillView}
				nodes={plan.nodes}
				folded={foldedOf(plan)}
				rootId={drillRootId}
				currentNodeIds={plan.current_node_ids}
				nextNodeIds={plan.next_node_ids}
				hostKey={key}
				onBack={() => (drillRootId = null)}
				{onOpenChild}
				onOpenArtifact={openAsk}
				{onOpenLegacyDiscussion}
			/>
		{:else}
			{#if allFolded}
				<div class="plan-state empty">
					<strong>Nothing open</strong>
					<p>Finished work is collapsed below and still counts as done.</p>
				</div>
			{/if}
			{#key paneView.key}
				<PlanTree
					bind:this={overviewTree}
					nodes={plan.nodes}
					folded={foldedOf(plan)}
					currentNodeIds={plan.current_node_ids}
					nextNodeIds={plan.next_node_ids}
					isChildExpanded={childExpansion.isExpanded}
					childRowState={childExpansion.state}
					onToggleChild={childExpansion.toggle}
					onRetryChild={childExpansion.retry}
					onDrill={(nodeId) => (drillRootId = nodeId)}
					paneView={paneView.view}
					{userActionBusy}
					onUserAction={applyUserAction}
					{onOpenChild}
					onOpenArtifact={openAsk}
					{onOpenLegacyDiscussion}
				/>
			{/key}
		{/if}
	{:else}
		<div class="plan-state empty"><strong>No plan yet</strong></div>
	{/if}
</div>

<style>
	.thread-plan-pane { display: flex; flex-direction: column; gap: var(--space-2); }
	.thread-plan-pane:focus { outline: none; }
	.decision-row { display: flex; align-items: center; gap: var(--space-2); min-height: 38px; padding: 6px 8px; border: 0; border-radius: var(--radius-xs); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
	@media (hover: hover) { .decision-row:hover { background: var(--bg-hover); } }
	.decision-mark { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; flex: none; border: 1px solid var(--amber); border-radius: var(--radius-circle); color: var(--amber); font-size: var(--text-caption); font-weight: var(--fw-bold); }
	.decision-title { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-medium); }
	.decision-open { flex: none; color: var(--text-tertiary); font-size: var(--text-caption); }
	.decision-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.decision-indicator {
		display: flex; align-items: center; min-height: 38px; padding: 6px 8px 6px 4px; border: 0;
		border-radius: var(--radius-xs); background: transparent; color: var(--amber-text);
		font: inherit; font-size: var(--text-footnote); text-align: left; cursor: pointer;
	}
	@media (hover: hover) { .decision-indicator:hover:not(:disabled) { background: var(--bg-hover); } }
	@media (any-pointer: coarse) { .decision-indicator { min-height: 44px; } }
	.decision-indicator b { margin-right: 0.3em; color: var(--amber-text); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
	.decision-indicator span { margin-left: auto; padding: 0 2px 0 var(--space-1); color: var(--text-tertiary); font-size: var(--text-caption); }
	.decision-indicator:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
	.decision-indicator:disabled { cursor: wait; opacity: 0.72; }
	.decision-message { margin: 0; padding: 0 var(--space-2); color: var(--text-tertiary); font-size: var(--text-caption); }
	.plan-state {
		display: flex; min-height: 96px; padding: var(--space-3); flex-direction: column;
		align-items: center; justify-content: center; gap: var(--space-2);
		color: var(--text-tertiary); text-align: center;
	}
	.plan-state.error { color: var(--red); }
	.plan-state strong { color: var(--text-primary); font-size: var(--text-footnote); }
	.plan-state p { margin: 0; font-size: var(--text-caption); }
	.status-banner {
		display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
		padding: var(--space-2); border: 1px solid var(--amber-line); border-radius: var(--radius-sm);
		background: var(--amber-bg); color: var(--text-primary); font-size: var(--text-caption);
	}
	.spinner {
		width: 18px; height: 18px; border: 2px solid var(--border); border-top-color: var(--violet);
		border-radius: var(--radius-circle); animation: spin var(--dur-hero-plus) linear infinite;
	}
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
</style>
