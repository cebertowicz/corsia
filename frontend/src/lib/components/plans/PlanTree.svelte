<script lang="ts">
	import type { OperatorArtifactLink, OperatorNode, UserActionHandler } from '$lib/api/plans';
	import { buildPlanTreeView, childPlanView, indexPlanNodes } from './planTreeFilter';
	import { isVisibleFinished, levelOf } from './planTreeFold';
	import { buildFoldLevels, foldRemaining, type FoldedWork } from './planFoldLevels';
	import { createFoldHistory } from './foldHistory.svelte';
	import { formatMoment, minuteClock } from '$lib/time';
	import { createFoldWave, createPaneHold, type PaneView } from './foldMotion.svelte';
	import { createFocusKeeper } from './focusKeeper.svelte';
	import { indentVar } from './planIndent';
	import { createPlanTreeLayout } from './planTreeLayout.svelte';
	import { createPlanTreeRows } from './planTreeRows.svelte';
	import { blockSlot } from '$lib/slotMotion';
	import PlanRowFace from './PlanRowFace.svelte';
	import PlanFoldMark from './PlanFoldMark.svelte';
	import PlanFoldHistory from './PlanFoldHistory.svelte';
	import PlanGroupFace from './PlanGroupFace.svelte';
	import PlanLevelToggle from './PlanLevelToggle.svelte';
	import PlanRowActions from './PlanRowActions.svelte';
	import type { ScheduledView } from './planDisplay';
	import { carriedByParent, rolledLive } from './planLiveStep';
	import { personStepInstructionsDocument } from './nodeDetailsText';
	import PlanRowDetails from './PlanRowDetails.svelte';
	import PlanTaskMenuHost from './PlanTaskMenuHost.svelte';
	import { createTaskMenu } from './taskMenu.svelte';
	import { createRowDisclosure } from './rowDisclosure.svelte';
	import { createPlanTreeReveal } from './planTreeReveal.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import PlanTree from './PlanTree.svelte';
	import { EMPTY_CHILD_STATE, type ChildRowState } from './childExpansion.svelte';
	/** A child plan renders whole: its depth continues its parent's, with no drill boundary. */
	const CHILD_MAX_DEPTH = Number.MAX_SAFE_INTEGER;

	let {
		nodes,
		planId = '',
		depthOffset = 0,
		currentNodeIds = [],
		nextNodeIds = [],
		maxDepth = 6,
		expandBranchesByDefault = false,
		isChildExpanded = () => false,
		childRowState = () => EMPTY_CHILD_STATE,
		onToggleChild = () => {},
		onRetryChild = () => {},
		onDrill = () => {},
		onOpenChild = () => {},
		onOpenArtifact = () => {},
		onOpenLegacyDiscussion = () => {},
		userActionBusy = false,
		onUserAction,
		paneView = null,
		folded = null,
	}: {
		nodes: OperatorNode[];
		/** Scopes DOM ids, flip keys and fold levels when a child plan nests in this tree. */
		planId?: string;
		/** The depth this tree's roots render at: a nested child plan's parent row + 1. */
		depthOffset?: number;
		currentNodeIds?: string[];
		nextNodeIds?: string[];
		maxDepth?: number;
		/** Drill-down owns a deliberately expanded focused subtree; overview does not. */
		expandBranchesByDefault?: boolean;
		isChildExpanded?: (nodeId: string) => boolean;
		childRowState?: (nodeId: string) => ChildRowState;
		onToggleChild?: (nodeId: string) => void;
		onRetryChild?: (nodeId: string) => void;
		onDrill?: (nodeId: string) => void;
		onOpenChild?: (nodeId: string, threadId: string, title: string, stepId?: string) => void;
		onOpenArtifact?: (artifact: OperatorArtifactLink, nodeTitle: string) => void;
		onOpenLegacyDiscussion?: (artifactId: string, title: string) => void;
		userActionBusy?: boolean;
		onUserAction?: UserActionHandler;
		/** What the user has seen of this plan in the pane; only the pane's own tree passes it. */
		paneView?: PaneView | null;
		/** The server's folds for this plan and where its older finished rows are read from; none: nothing is folded. */
		folded?: FoldedWork | null;
	} = $props();

	// Right-click, Shift+F10 or a long-press opens the row's actions, when it has any.
	const taskMenu = createTaskMenu(
		(node) => !userActionBusy && !!onUserAction && node.user_actions.length > 0,
	);

	// Which tasks are quietly scheduled depends on the clock, so the tree re-arranges on its minute tick.
	const now = $derived($minuteClock);

	// Finished work new since the user last looked stays open, then folds (`foldMotion`).
	const hold = createPaneHold(() => nodes, () => paneView);
	// Only finished work folds: an open task, however far off its wait ends, stays a row.
	const shownNodes = $derived(hold.nodes);
	// Each level's fold row reads the server's counts; its older rows are fetched when the user opens them.
	const history = createFoldHistory({ planId: () => folded?.planId ?? '', rootParent: () => folded?.rootParent ?? null });
	const foldView = $derived(buildFoldLevels({ nodes: shownNodes, held: hold.held, work: folded, history: history.level, maxDepth }));
	// A row the history route fetched carries no details; opening it reads the node (`PlanHistoryDetails`).
	const historyIds = $derived(new Set(foldView.extra.map((row) => row.id)));
	const treeNodes = $derived(foldView.extra.length > 0 ? [...shownNodes, ...foldView.extra] : shownNodes);
	const index = $derived(indexPlanNodes(treeNodes, { maxDepth, currentNodeIds, deepFolded: foldView.deep, now: now.getTime() }));

	const { isBranchExpanded, toggleBranch, openBranches, detailsExpanded, toggleDetails } = createRowDisclosure(
		(nodeId) => expandBranchesByDefault || index.defaultExpandedBranches.has(nodeId),
	);
	const reveal = createPlanTreeReveal({
		maxDepth: () => maxDepth,
		byId: () => index.byId,
		levels: () => foldView.levels,
		items: () => items,
		openBranches,
		history,
	});

	// Row selection and disclosure eligibility come from one pure rule set, so
	// no row can render a chevron that discloses nothing or bypass the
	// collapsed-ancestor filter. The per-level fold row is the one control that
	// hides finished work; there is no separate plan-wide toggle.
	const view = $derived(buildPlanTreeView({
		nodes: treeNodes, index, maxDepth, isBranchExpanded, levels: foldView.levels, revealed: reveal.revealed,
		now: now.getTime(), isScheduledOpen: reveal.isScheduledOpen,
	}));
	const visibleNodes = $derived(view.visibleNodes);
	const items = $derived(view.items);
	const branchParents = $derived(view.branchParents);
	const boundaryParents = $derived(index.boundaryParents);
	const scope = $derived(planId ? `${planId}:` : '');

	/** Open what hides one row (branches, folded pages); false past this tree's depth, so the caller drills. */
	export function revealNode(nodeId: string): boolean {
		return reveal.revealNode(nodeId);
	}

	/** Open one row's details, as pressing its row does. */
	export function openDetails(nodeId: string): void {
		if (!detailsExpanded(nodeId)) reveal.byRequest(() => toggleDetails(nodeId));
	}

	/** Open a node the read did not list (older finished work): page its levels' history down to it, then reveal it. */
	export async function revealFolded(nodeId: string): Promise<boolean> {
		return reveal.revealFolded(nodeId);
	}

	/** Focus one rendered row, this tree's or an expanded subagent plan's nested in it; false when none is rendered. */
	export function focusRow(nodeId: string): boolean {
		return rowControls.focusRow(nodeId);
	}

	// The controller owns person-requested disclosure and page motion; keep this gate local so transition wiring stays explicit.
	const userDisclosure = $derived(reveal.userDisclosure);
	const pendingFoldLevel = $derived(reveal.pendingFoldLevel);

	// The fold wave (`foldMotion`): rows that fold or sink since the last render glide as one wave.
	let treeElement = $state<HTMLElement | null>(null);
	const { indentAt, depthStyle } = createPlanTreeLayout(
		() => treeElement, (depth, width, reserve) => indentVar(depth, width, reserve),
	);
	let childTrees = $state<Record<string, PlanTree | null>>({}); // Expanded nested subagent plans, by nesting step.
	const rowControls = createPlanTreeRows({
		scope: () => scope,
		visibleNodes: () => visibleNodes,
		isBranchExpanded,
		element: () => treeElement,
		childTrees: () => childTrees,
	});
	const live = $derived(rolledLive(shownNodes)); // One roll-up for every row's spin and live line.
	const below = $derived(carriedByParent(visibleNodes, live.threads));
	/** Every thread this tree's rendered rows carry: a nesting step drops a line its subagent plan already shows. */
	export function carriedThreads(): string[] {
		return visibleNodes.flatMap((row) => live.threads.get(row.id) ?? []);
	}
	/** What the rows rendered under a row already show: its child rows, and its expanded subagent plan's. */
	function carriedUnder(nodeId: string): string[] {
		const nested = isChildExpanded(nodeId) ? childTrees[nodeId]?.carriedThreads() ?? [] : [];
		return [...(below.get(nodeId) ?? []), ...nested];
	}
	createFocusKeeper(() => treeElement, () => items);
	createFoldWave({
		nodes: () => shownNodes,
		items: () => items,
		element: () => treeElement,
		isFinished: (node) => isVisibleFinished(node, index.openBlocked),
		keyOf: (node) => ({ id: `${scope}${node.id}`, level: `${scope}${levelOf(node, index.byId)}` }),
	});

</script>

<!-- A row's ask opens while it is open: raised, or deferred by a pause or a put-off. -->
{#snippet row(node: OperatorNode, nest: number, view?: ScheduledView)}
		{@const ask = node.artifacts.find((artifact) => artifact.role === 'attention' && (artifact.attention_state === 'raised' || artifact.attention_state === 'deferred'))}
		{@const instructions = personStepInstructionsDocument(node)}
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<div
			class="tree-row" role="listitem"
			in:blockSlot|global={{ off: !userDisclosure }} out:blockSlot|global={{ off: !userDisclosure }}
			class:details-open={detailsExpanded(node.id)}
			class:has-menu={!!onUserAction && node.user_actions.length > 0}
			{...taskMenu.rowHandlers(node)}
			id={rowControls.rowId(node.id)}
			data-flip-key={`${scope}${node.id}`}
			style={depthStyle(depthOffset + node.depth + nest)}
			data-depth={depthOffset + node.depth + nest}
			aria-level={depthOffset + node.depth + nest + 1}
			aria-current={currentNodeIds.includes(node.id) ? 'step' : undefined}
		>
			<!-- The row body is the details disclosure (a person step with an instructions
			     document opens it instead): status, title and subline are one control. A branch's
			     done/total count and chevron are its separate expand control, a sibling. -->
			<button
				class="row-body"
				type="button"
				aria-expanded={instructions ? undefined : detailsExpanded(node.id)}
				aria-controls={instructions ? undefined : rowControls.detailsId(node.id)}
				aria-haspopup={onUserAction && node.user_actions.length > 0 ? 'menu' : undefined}
				title={instructions ? 'Open instructions' : `${detailsExpanded(node.id) ? 'Hide' : 'Show'} details`}
				onclick={() => instructions ? onOpenArtifact(instructions, node.title) : reveal.byRequest(() => toggleDetails(node.id))}
			>
				<PlanRowFace
					{node} isNext={nextNodeIds.includes(node.id)} showProgress={!branchParents.has(node.id)} detailsOpen={detailsExpanded(node.id)}
					scheduledView={view} threads={live.threads.get(node.id)} moves={live.moving.has(node.id)} owners={live.owners} carriedBelow={carriedUnder(node.id)}
				/>
			</button>
			<PlanRowActions
				{node} {ask} isBranch={branchParents.has(node.id)} branchExpanded={isBranchExpanded(node.id)} detailsOpen={detailsExpanded(node.id)}
				controls={rowControls.controlledRowIds(node.id)} childExpanded={isChildExpanded(node.id)}
				onToggleBranch={() => reveal.byRequest(() => toggleBranch(node.id))} {onOpenArtifact} {onOpenLegacyDiscussion}
				onToggleChild={() => onToggleChild(node.id)}
				onOpenChild={() => onOpenChild(node.id, node.child!.thread_id, node.title)}
			/>
		</div>
		{#if detailsExpanded(node.id)}
			<div class="details-slot" in:blockSlot|global={{ off: !userDisclosure }} out:blockSlot|global={{ off: !userDisclosure }}>
				<PlanRowDetails
					{node} id={rowControls.detailsId(node.id)} planId={folded?.planId ?? ''} fromHistory={historyIds.has(node.id)}
					indent={indentAt(depthOffset + node.depth + nest)}
				/>
			</div>
		{/if}
		{#if node.child?.available && isChildExpanded(node.id)}
			{@const child = childRowState(node.id)}
			{#if child.status === 'loading' || child.status === 'idle'}
				<div class="tree-row child-row" style={depthStyle(depthOffset + node.depth + nest + 1)}>Loading…</div>
			{:else if child.status === 'not-found' || child.status === 'forbidden'}
				<!-- Archived workers still read. The server does not say why it refused, so neither do we. -->
				<div class="tree-row child-row" style={depthStyle(depthOffset + node.depth + nest + 1)}>This subagent’s plan isn’t available.</div>
			{:else if child.status === 'error' || child.status === 'unauthorized'}
				<div class="tree-row child-row child-error" role="alert" style={depthStyle(depthOffset + node.depth + nest + 1)}>
					<span>{child.status === 'unauthorized' ? 'Your session ended. Sign in again to see this subagent’s plan.' : 'Couldn’t reach CORSIA for this subagent’s plan.'}</span>
					<Button variant="ghost" size="sm" onclick={() => onRetryChild(node.id)}>Retry</Button>
				</div>
			{:else}
				{#if child.status === 'stale'}
					<div class="tree-row child-row child-stale" role="status" style={depthStyle(depthOffset + node.depth + nest + 1)}>
						<span>Showing last known subagent plan.</span>
						<Button variant="ghost" size="sm" onclick={() => onRetryChild(node.id)}>Retry</Button>
					</div>
				{/if}
				<!-- The child plan is this same tree, nested one level under its step: the
				     same rows, folds, details and subagent controls, whose leaf opens name this
				     step. No drill boundary; user actions stay off (they mutate this pane's plan). -->
				{@const childView = childPlanView(child)}
				<div class="child-tree" role="listitem">
					<PlanTree
						nodes={childView.nodes}
						folded={childView.work}
						planId={child.planId ?? `child-of-${node.id}`}
						depthOffset={depthOffset + node.depth + nest + 1}
						currentNodeIds={child.currentNodeIds}
						nextNodeIds={child.nextNodeIds}
						maxDepth={CHILD_MAX_DEPTH}
						{isChildExpanded}
						{childRowState}
						{onToggleChild}
						{onRetryChild}
						bind:this={childTrees[node.id]}
						onOpenChild={(nodeId, threadId, title) => onOpenChild(nodeId, threadId, title, node.id)}
						{onOpenArtifact}
						{onOpenLegacyDiscussion}
					/>
				</div>
			{/if}
		{/if}
		{#if boundaryParents.has(node.id)}
			<button class="drill-row" style={depthStyle(depthOffset + node.depth + nest + 1)} onclick={() => onDrill(node.id)}>
				View deeper steps →
			</button>
		{/if}
{/snippet}

<div class="plan-tree" role="list" aria-label="Plan steps" bind:this={treeElement}>
	<!-- Every item carries an out transition: Svelte 5 destroys a keyed batch only when all its removed items have one, so a bare Scheduled or fold row would keep a closed group's rows on screen. -->
	{#each items as item (item.key)}
		{#if item.kind === 'fold'}
			<!-- A level's finished work, gathered at its bottom: still counted as
			     done, opened in pages newest first, never a second plan view. -->
			<div
				class="tree-row fold-row" role="listitem" in:blockSlot|global={{ off: !userDisclosure }} out:blockSlot|global={{ off: !userDisclosure }}
				data-flip-key={`${scope}${item.key}`} data-fold-level={`${scope}${item.level}`}
				style={depthStyle(depthOffset + item.depth)} data-depth={depthOffset + item.depth}
				aria-level={depthOffset + item.depth + 1}
			>
				<span class="row-body">
					<PlanFoldMark counts={item.view.counts} />
				</span>
				<PlanLevelToggle
					shown={item.shown} total={foldRemaining(item.view)} page={reveal.pageSize} noun="finished steps" pending={pendingFoldLevel === item.level}
					controls={item.view.rows.slice(0, item.shown).map((folded) => rowControls.rowId(folded.id)).join(' ')}
					onShow={() => reveal.pageFold(item.level)} onCollapse={() => reveal.collapseFold(item.level)}
				/>
			</div>
			<PlanFoldHistory
				history={item.view.history} indent={indentAt(depthOffset + item.depth)}
				exhausted={item.view.rows.length < item.view.total} onRetry={() => reveal.pageFold(item.level)}
			/>
		{:else if item.kind === 'scheduled'}
			<!-- A level's quietly scheduled tasks, gathered after its open rows: opened all at once. -->
			<div
				class="tree-row scheduled-row" role="listitem" in:blockSlot|global={{ off: !userDisclosure }} out:blockSlot|global={{ off: !userDisclosure }} data-flip-key={`${scope}${item.key}`}
				style={depthStyle(depthOffset + item.depth)} data-depth={depthOffset + item.depth}
				aria-level={depthOffset + item.depth + 1}
			>
				<span class="row-body"><PlanGroupFace /></span>
				<PlanLevelToggle
					single shown={item.open ? item.members.length : 0} total={item.members.length} noun="scheduled tasks"
					controls={item.members.map((member) => rowControls.rowId(member.id)).join(' ')}
					onShow={() => reveal.openScheduled(item.level)}
					onCollapse={() => reveal.collapseScheduled(item.level)}
				/>
			</div>
		{:else if item.kind === 'cluster'}
			<!-- Tasks sharing one moment: one time line, their rows listed beneath it. -->
			<div class="cluster" role="listitem" style={depthStyle(depthOffset + item.depth + 1)}>
				<div class="time-line">{item.members.length}<span class="sr-only">{' tasks'}</span> at {formatMoment(new Date(item.at), now, { clock: true, plain: true })}</div>
				<div role="list">
					{#each item.members as member (member.id)}
						{@render row(member, 1, 'reason')}
					{/each}
				</div>
			</div>
		{:else}
			{@render row(item.node, item.nest ?? 0, item.view)}
		{/if}
	{/each}
</div>

<PlanTaskMenuHost menu={taskMenu} {onUserAction} />

<style>
	/* Relative, so a fold wave's ghosts lay over the rows they left. */
	/* The reserve tracks the same <=620px breakpoint as the 44px chevron and chips: desktop furniture is measured at 110px plus a two-digit count margin; touch keeps its 148px reserve. */
	.plan-tree { --plan-indent-reserve: 122px; position: relative; display: flex; flex-direction: column; }
	@media (max-width: 620px) { .plan-tree { --plan-indent-reserve: 148px; } }
	.tree-row, .drill-row {
		--plan-row-line-height: calc(var(--text-subhead) * var(--lh-normal));
		--plan-row-action-height: 28px;
		display: flex; flex-wrap: nowrap; align-items: center; gap: var(--space-2); min-height: 38px;
		padding: 6px 8px 6px calc(8px + var(--indent)); border-radius: var(--radius-xs);
		width: 100%;
	}
	/* A long-press opens the task menu, so it must not also select text or raise the iOS callout. */
	@media (pointer: coarse) {
		.tree-row.has-menu { -webkit-touch-callout: none; -webkit-user-select: none; user-select: none; }
	}
	.drill-row { border: 0; background: transparent; color: var(--accent-text); font-size: var(--text-footnote); text-align: left; cursor: pointer; }
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last thing tapped. */
	@media (hover: hover) {
		.drill-row:hover { color: var(--text-primary); background: var(--bg-hover); }
	}
	/* The primary row body. As a disclosure button it is the whole comfortable
	   hit area; as a static span it lays out identically, so a leaf row and a
	   branch row share one baseline and one left edge. */
	.row-body {
		display: flex; flex: 1 1 0; min-width: 0; align-items: center; gap: var(--space-2);
		padding: 0; border: 0; border-radius: var(--radius-xs);
		background: transparent; color: inherit; font: inherit; text-align: left;
	}
	/* A disclosed title may wrap; all furniture stays on its first line. */
	.tree-row.details-open, .tree-row.details-open .row-body { align-items: flex-start; }
	button.row-body { cursor: pointer; }
	/* A folding row's inert copy, laid over the tree while it travels. */
	.plan-tree :global(.fold-ghost) {
		z-index: 1; pointer-events: none; box-shadow: var(--shadow-chip); will-change: transform, opacity;
	}
	@media (hover: hover) {
		button.row-body:hover { background: var(--bg-hover); }
	}
	button.row-body:focus-visible, .drill-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* The one line above tasks that share a moment, at their left edge. */
	.time-line {
		padding: 6px 8px 0 calc(4px + var(--indent)); overflow: hidden; color: var(--text-secondary);
		font-size: var(--text-caption); font-weight: var(--fw-medium); text-overflow: ellipsis; white-space: nowrap;
	}
	.sr-only {
		position: absolute; width: 1px; height: 1px; overflow: hidden;
		clip: rect(0 0 0 0); white-space: nowrap;
	}
	/* Keep the details section's margins inside its measured motion slot. */
	.details-slot { display: flow-root; }
	.child-row { color: var(--text-secondary); }
	/* A notice with a retry is prose, not a plan row: it may wrap. */
	.child-error { flex-wrap: wrap; color: var(--red-text); justify-content: space-between; }
	.child-stale { flex-wrap: wrap; color: var(--amber); justify-content: space-between; }
	/* The details target is the whole row body, so it carries the touch minimum. */
	@media (max-width: 620px) { .tree-row, .drill-row { --plan-row-action-height: 44px; } .drill-row, button.row-body { min-height: 44px; } }
</style>
