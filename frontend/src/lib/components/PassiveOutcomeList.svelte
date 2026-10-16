<script lang="ts">
	import type { UpdateCard } from '$lib/api/threadUpdates';
	import type { OperatorNode } from '$lib/api/plans';
	import { focusPlanNode } from '$lib/planNodeFocus';
	import { emitProductEvent } from '$lib/product_events';
	import { planKeySet, planQuery } from '$lib/stores/planQuery.svelte';
	import type { OperatorKey } from '$lib/stores/planKeys';
	import { breadcrumbName, indexNodes, taskDetail } from './completedTaskDetail';
	import OutcomeTime from './OutcomeTime.svelte';

	let { cards, id }: { cards: UpdateCard[]; id: string } = $props();
	let expanded = $state(false);
	let openCards = $state<Set<number>>(new Set());
	const failed = $derived(cards.filter((card) => card.kind === 'failed').length);
	const closed = $derived(cards.filter((card) => card.kind === 'closed').length);
	const completed = $derived(cards.length - failed - closed);
	// Counts run completed, closed, failed; only the failed segment is coloured.
	const lead = $derived(completed > 0
		? `${completed} ${completed === 1 ? 'task completed' : 'tasks completed'}${closed ? ` · ${closed} closed` : ''}`
		: closed > 0 ? `${closed} ${closed === 1 ? 'task closed' : 'tasks closed'}` : '');
	const failure = $derived(failed === 0 ? '' : lead ? `${failed} failed` : `${failed} ${failed === 1 ? 'task failed' : 'tasks failed'}`);
	const label = $derived(lead && failure ? `${lead} · ${failure}` : lead || failure);

	// The thread's plan read the drawer already loads; read only once a row is opened.
	const threadId = $derived(cards[0]?.thread_id ?? null);
	const readKey: OperatorKey | null = $derived(openCards.size > 0 && threadId ? { kind: 'thread', id: threadId } : null);
	const read = planQuery(() => readKey);
	const readReady = $derived(read.payload != null && (read.status === 'ready' || read.status === 'stale'));
	const planNodes = $derived<OperatorNode[]>(read.payload?.plans.flatMap((plan) => plan.nodes) ?? []);
	const byId = $derived(indexNodes(planNodes));
	// A task missing from this read may sit in a subagent's plan nested under one of this thread's steps.
	const missingOpen = $derived(readReady && cards.some((card) => openCards.has(card.id) && card.node_id && !byId.has(card.node_id)));
	const childSteps = $derived(missingOpen ? planNodes.filter((node) => node.child?.available) : []);
	const childKeys = $derived<OperatorKey[]>(childSteps.map((node) => ({ kind: 'child', id: node.child!.thread_id })));
	const children = planKeySet(() => childKeys, () => readKey);
	const holder = (nodeId: string): { stepId: string } | 'loading' | 'error' | null => {
		let loading = false;
		let errored = false;
		for (const step of childSteps) {
			const entry = children.state({ kind: 'child', id: step.child!.thread_id });
			if (entry.payload?.plans.some((plan) => plan.nodes.some((node) => node.id === nodeId))) return { stepId: step.id };
			if (entry.status === 'idle' || entry.status === 'loading') loading = true;
			else if (!entry.payload) errored = true;
		}
		return loading ? 'loading' : errored ? 'error' : null;
	};

	/** Retry the thread read and any child plan read that failed. */
	function retryReads(): void {
		read.retry();
		for (const step of childSteps) {
			const key: OperatorKey = { kind: 'child', id: step.child!.thread_id };
			const entry = children.state(key);
			if (entry.status !== 'ready' && entry.status !== 'stale' && entry.status !== 'idle' && entry.status !== 'loading') children.retry(key);
		}
	}

	function toggle(card: UpdateCard): void {
		const next = new Set(openCards);
		if (!next.delete(card.id)) next.add(card.id);
		openCards = next;
	}

	function open(card: UpdateCard, stepId: string | null = null): void {
		if (!card.node_exists || !card.node_id) return;
		emitProductEvent('update_card', 'plan_open');
		void focusPlanNode(card.thread_id, card.node_id, stepId);
	}

	/** Where the expansion's content comes from: this read, a nested subagent plan, or neither yet. */
	function view(card: UpdateCard) {
		if (!card.node_id) return { state: 'none' as const };
		if (!readReady) return { state: read.status === 'idle' || read.status === 'loading' ? 'loading' as const : 'error' as const };
		const detail = taskDetail(byId, card.node_id);
		if (detail) return { state: 'detail' as const, detail, stepId: null };
		const where = holder(card.node_id);
		if (where === 'loading' || where === 'error') return { state: where };
		return where ? { state: 'outside' as const, stepId: where.stepId } : { state: 'unplaced' as const };
	}

	let toggleButton: HTMLButtonElement | undefined;
	// A row that loses its button because no plan holds its task must not drop keyboard focus to the page.
	$effect(() => {
		const unplaced = cards.some((card) => openCards.has(card.id) && view(card).state === 'unplaced');
		if (unplaced && (!document.activeElement || document.activeElement === document.body)) toggleButton?.focus();
	});

	const rowName = (card: UpdateCard) => card.kind === 'closed' ? 'Closed: ' : '';
</script>

{#snippet glyph(closed: boolean, failed = false)}
	<svg class="outcome-icon" class:closed class:failed width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
		{#if closed}<path d="M6 6l12 12M18 6L6 18" />{:else}<path d="m5 12 4 4L19 6" />{/if}
	</svg>
{/snippet}

{#snippet rowIcon(card: UpdateCard)}
	{#if card.kind === 'failed'}
		<svg class="outcome-icon failed" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7.5v5.5M12 16.5h.01" /></svg>
	{:else}
		{@render glyph(card.kind === 'closed')}
	{/if}
{/snippet}

{#snippet snapshot(card: UpdateCard)}
	{#if card.kind === 'failed'}<span class="failed-count">Failed</span>{/if}
	<span class="sr-only">{rowName(card)}</span>
	<span class="node-title" title={card.title || 'Plan item'}>{card.title || 'Plan item'}</span>
	{#if card.kind === 'done_follow_up'}<span class="qualifier">· Follow-up added</span>{/if}
	<span class="when"><OutcomeTime value={card.created_at} compact /></span>
{/snippet}

<section class="passive-outcomes" aria-label={label}>
	<button type="button" class="passive-toggle" bind:this={toggleButton} aria-expanded={expanded} aria-controls={id} onclick={() => { expanded = !expanded; if (!expanded) openCards = new Set(); }}>
		{@render glyph(completed === 0, completed === 0 && closed === 0 && failed > 0)}
		<span>{lead}{lead && failure ? ' · ' : ''}{#if failure}<span class="failed-count">{failure}</span>{/if}</span>
		<svg class="passive-chevron" class:expanded width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
	</button>
	<ul id={id} hidden={!expanded}>
		{#each cards as card (card.id)}
			{@const isOpen = openCards.has(card.id)}
			{@const shown = isOpen ? view(card) : null}
			<li data-update-id={card.id}>
				{#if card.node_exists && card.node_id && shown?.state !== 'unplaced'}
					<button type="button" class="row" aria-expanded={isOpen} aria-controls={`${id}-${card.id}`} onclick={() => toggle(card)}>
						{@render rowIcon(card)}{@render snapshot(card)}
						<svg class="row-chevron" class:expanded={isOpen} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
					</button>
				{:else}<div class="row deleted">{@render rowIcon(card)}{@render snapshot(card)}</div>{/if}
				{#if shown && shown.state !== 'unplaced'}
					<div class="detail" id={`${id}-${card.id}`}>
						{#if shown.state === 'detail'}
							{@const detail = shown.detail}
							{#if detail.crumbs.length > 0 || detail.gap}
								<p class="crumbs" aria-label={breadcrumbName(detail)}>
									<span aria-hidden="true">
										{#if detail.gap}… › {/if}{detail.crumbs[0]}{#if detail.crumbs.length > 2}<span class="crumb-mid"> › {detail.crumbs.slice(1, -1).join(' › ')}</span><span class="crumb-gap"> › …</span>{/if}{#if detail.crumbs.length > 1} › {detail.crumbs[detail.crumbs.length - 1]}{/if}
									</span>
								</p>
							{/if}
							{#if detail.text}<p class="text">{detail.text}</p>{/if}
						{:else if shown.state === 'loading'}
							<div class="skeleton" role="status" aria-label="Loading details"></div>
						{:else if shown.state === 'error'}
							<p class="text">Couldn’t load details · <button type="button" class="link" onclick={retryReads}>Retry</button></p>
						{/if}
						<button type="button" class="link open-plan" onclick={() => open(card, shown.state === 'outside' ? shown.stepId : null)}>Open in plan</button>
					</div>
				{/if}
			</li>
		{/each}
	</ul>
</section>

<style>
	.passive-outcomes { max-width: 760px; margin: 0 auto 14px; color: var(--text-secondary); font-size: var(--text-footnote); }
	.passive-toggle { width: 100%; min-height: 44px; display: flex; align-items: center; gap: 7px; padding: 8px 8px 8px 4px; border: 0; background: transparent; color: inherit; font: inherit; font-weight: var(--fw-medium); text-align: left; cursor: pointer; }
	.outcome-icon { color: var(--green-text); flex: 0 0 auto; }
	.outcome-icon.closed { color: var(--text-tertiary); }
	.outcome-icon.failed, .failed-count { color: var(--red-text); }
	.passive-chevron { margin-left: auto; flex: 0 0 auto; color: var(--text-tertiary); transition: transform var(--dur-tight); }
	.passive-chevron.expanded { transform: rotate(90deg); }
	ul { list-style: none; margin: 0 0 0 22px; padding: 0 4px; }
	li { padding: 0; }
	.row { width: 100%; min-height: 32px; display: flex; align-items: center; gap: 7px; padding: 4px 0; border: 0; background: transparent; color: var(--text-primary); font: inherit; text-align: left; cursor: pointer; }
	.row.deleted { cursor: default; }
	.node-title { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.row:hover .node-title { text-decoration: underline; }
	.row.deleted:hover .node-title { text-decoration: none; }
	.qualifier { flex: 0 0 auto; color: var(--text-secondary); white-space: nowrap; }
	.when { margin-left: auto; flex: 0 0 auto; padding-left: 8px; white-space: nowrap; }
	.when :global(time) { white-space: nowrap; }
	.row-chevron { flex: 0 0 auto; color: var(--text-tertiary); transition: transform var(--dur-tight); }
	.row-chevron.expanded { transform: rotate(90deg); }
	.detail { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; padding: 0 0 8px 23px; }
	.detail p { margin: 0; }
	.crumbs { color: var(--text-tertiary); overflow-wrap: anywhere; }
	.crumb-gap { display: none; }
	.text { display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; color: var(--text-secondary); overflow-wrap: anywhere; }
	.skeleton { width: 60%; height: 12px; border-radius: var(--radius-2xs); background: var(--bg-hover); }
	.link { padding: 0; border: 0; background: transparent; color: var(--accent-text); font: inherit; cursor: pointer; }
	.link:hover { text-decoration: underline; }
	.open-plan { min-height: 28px; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	.row:focus-visible, .passive-toggle:focus-visible, .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-2xs); }
	@media (pointer: coarse) {
		.row { min-height: 44px; }
		.open-plan { min-height: 44px; }
	}
	/* Narrow width: the middle ancestors give way to an ellipsis; the full path stays in the name. */
	@media (max-width: 480px) {
		.crumb-mid { display: none; }
		.crumb-gap { display: inline; }
	}
</style>
